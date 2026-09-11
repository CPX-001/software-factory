"""Synthetic telemetry only: no credentials, network, or model calls."""
import json
import time
import unittest

from openai_codex.errors import InternalRpcError, MethodNotFoundError
from openai_codex.generated.v2_all import GetAccountRateLimitsResponse
from pydantic import ValidationError

from factory.execution_contract import DEFAULT_POLICY
from factory.quota import diagnose, normalize, quota_guard
from factory.registry import FactoryError


class QuotaTests(unittest.TestCase):
    def check_code(self, code, value, *, now=1000):
        with self.assertRaises(FactoryError) as error:
            quota_guard(value, DEFAULT_POLICY, now=now)
        self.assertEqual(error.exception.code, code)
        self.assertTrue(error.exception.details['next_step'])

    def test_single_legacy_view_and_optional_window_duration_and_reset(self):
        value = normalize({'rateLimits': {'primary': {'usedPercent': 10}, 'secondary': None}}, observed_at=1000)
        quota_guard(value, DEFAULT_POLICY, now=1000)
        self.assertIsNone(value['buckets']['codex']['secondary'])
        self.assertIsNone(value['buckets']['codex']['primary']['resetsAt'])

    def test_real_shape_synthetic_numbers_secondary_absent_and_multiple_buckets(self):
        payload = {'rateLimits': {'limitId': 'codex', 'primary': {'usedPercent': 76, 'resetsAt': 2000}, 'secondary': None},
                   'rateLimitsByLimitId': {'other': {'primary': {'usedPercent': 0, 'resetsAt': 2000}}}}
        value = normalize(GetAccountRateLimitsResponse.model_validate(payload).model_dump(by_alias=True, mode='json'), observed_at=1000)
        self.check_code('quota_reserve', value)
        self.assertEqual(value['buckets']['other']['primary']['usedPercent'], 0)

    def test_keyed_view_precedes_single_view_and_never_selects_freest(self):
        value = normalize({'rateLimits': {'primary': {'usedPercent': 0}},
            'rateLimitsByLimitId': {'codex': {'secondary': {'usedPercent': 100}}, 'other': {'primary': {'usedPercent': 0}}}}, observed_at=1000)
        self.check_code('quota_exhausted', value)

    def test_every_reported_window_must_clear_reserve(self):
        value = normalize({'rateLimits': {'primary': {'usedPercent': 20}, 'secondary': {'usedPercent': 75}}}, observed_at=1000)
        self.check_code('quota_reserve', value)

    def test_missing_bucket_windows_values_and_stale_timestamps(self):
        for payload in ({}, {'rateLimits': {}}, {'rateLimits': {'primary': {}}},
                        {'rateLimits': {'limitId': 'other', 'primary': {'usedPercent': 0}}}):
            with self.subTest(payload=payload):
                self.check_code('quota_unknown', normalize(payload, observed_at=1000))
        for stamp in (0, 939, 1001, float('nan'), None):
            value = normalize({'rateLimits': {'primary': {'usedPercent': 0}}}, observed_at=1000)
            value['observed_at'] = stamp
            self.check_code('quota_unknown', value)
        self.check_code('quota_unknown', normalize({'rateLimits': {'primary': {'usedPercent': 0, 'resetsAt': 999}}}, observed_at=1000))

    def test_malformed_bucket_and_numeric_values_are_parsing_errors(self):
        for payload in ([], {'rateLimitsByLimitId': []}, {'rateLimits': []},
            {'rateLimitsByLimitId': {'codex': {'limitId': 'other'}}},
            *({'rateLimits': {'primary': {'usedPercent': v}}} for v in ('20', True, -1, 101, float('nan')))):
            with self.subTest(payload=payload), self.assertRaises(FactoryError) as error:
                normalize(payload)
            self.assertEqual(error.exception.code, 'quota_parsing')

    def test_credits_and_reset_offers_do_not_supply_unknown_quota_or_bypass_reserve(self):
        value = normalize({'rateLimits': {'primary': {'usedPercent': 100}, 'credits': {'unlimited': True, 'hasCredits': True}},
            'rateLimitResetCredits': {'availableCount': 99}}, observed_at=1000)
        self.check_code('quota_exhausted', value)
        self.assertNotIn('credits', value['buckets']['codex'])
        self.assertNotIn('rateLimitResetCredits', value)
        self.check_code('quota_exhausted', normalize({'rateLimits': {'spendControlReached': True}}, observed_at=1000))

    def test_notification_shape_uses_same_normalizer(self):
        value = normalize({'rateLimits': {'limitId': 'codex', 'primary': {'usedPercent': 23}}},
                          observed_at=1000, method='account/rateLimits/updated')
        quota_guard(value, DEFAULT_POLICY, now=1000)
        self.assertEqual(value['method'], 'account/rateLimits/updated')

    def test_failure_categories_retain_cause_without_credentials(self):
        examples = [
            (MethodNotFoundError(-32601, 'Method not found'), 'quota_method_unavailable'),
            (InternalRpcError(-32603, 'Not initialized SYNTHETIC_SECRET'), 'quota_configuration'),
            (InternalRpcError(-32603, 'failed to fetch codex rate limits: error sending request for url (https://service/?token=SYNTHETIC_SECRET)'), 'quota_transport'),
            (InternalRpcError(-32603, '401 Unauthorized Bearer SYNTHETIC_SECRET'), 'authentication'),
            (InternalRpcError(-32603, '503 service unavailable SYNTHETIC_SECRET'), 'quota_service'),
            (InternalRpcError(-32603, 'unrecognized SYNTHETIC_SECRET'), 'quota_rpc_error'),
            (ConnectionError('SYNTHETIC_SECRET'), 'quota_transport'),
            (TimeoutError('SYNTHETIC_SECRET'), 'quota_timeout'),
            (InternalRpcError(-32603, '429 rate limit exceeded SYNTHETIC_SECRET'), 'quota_exhausted')]
        for exc, expected in examples:
            with self.subTest(expected=expected):
                value = diagnose(exc)
                self.assertEqual(value.code, expected)
                self.assertNotIn('SYNTHETIC_SECRET', str(value) + json.dumps(value.details))
                self.assertEqual(value.details['exception'], type(exc).__name__)
        try:
            GetAccountRateLimitsResponse.model_validate({'rateLimits': {'primary': {'usedPercent': 'SYNTHETIC_SECRET'}}})
        except ValidationError as exc:
            value = diagnose(exc)
            self.assertEqual(value.code, 'quota_parsing')
            self.assertNotIn('SYNTHETIC_SECRET', str(value) + json.dumps(value.details))
