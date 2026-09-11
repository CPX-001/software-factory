"""Codex account telemetry, never an inference request or a paid fallback.

The SDK's typed request is the transport. Bucket dictionaries are deliberately
validated here too: SDK 0.147.0 types their values as Any. Diagnostics use an
allowlist of facts, never exception payloads, credentials or response bodies.
"""
import math
import re
import time
from typing import Annotated

from pydantic import Field, StrictBool, StrictInt
from openai_codex.generated.v2_all import GetAccountRateLimitsResponse, RateLimitSnapshot, RateLimitWindow

from .registry import FactoryError

METHOD = 'account/rateLimits/read'


class ObservedWindow(RateLimitWindow):
    # Do not let Pydantic turn True or "20" into a measured quota percentage.
    used_percent: Annotated[StrictInt | None, Field(alias='usedPercent')] = None
    resets_at: Annotated[StrictInt | None, Field(alias='resetsAt')] = None
    window_duration_mins: Annotated[StrictInt | None, Field(alias='windowDurationMins')] = None


class ObservedSnapshot(RateLimitSnapshot):
    primary: ObservedWindow | None = None
    secondary: ObservedWindow | None = None
    spend_control_reached: Annotated[StrictBool | None, Field(alias='spendControlReached')] = None


class QuotaResponse(GetAccountRateLimitsResponse):
    # Accept bucket-only/empty observations without fabricating the legacy view.
    # Missing measurements remain unknown; malformed values are parsing failures.
    rate_limits: Annotated[ObservedSnapshot | None, Field(alias='rateLimits')] = None


NEXT_STEPS = {
    'quota_method_unavailable': 'Use a pinned SDK/runtime that supports account/rateLimits/read.',
    'quota_configuration': 'Check the SDK initialization and private App Server configuration before resuming.',
    'quota_meter_unsupported': 'This execution mode supports the standard codex meter only; add a documented model/meter mapping before using a separate meter.',
    'authentication': 'Restore the local ChatGPT login/configuration; API keys are not an alternative.',
    'quota_transport': 'Restore network/TLS access for the detached controller and its isolated runtime, then resume.',
    'quota_service': 'Retry the read-only quota diagnostic when the Codex service is available.',
    'quota_timeout': 'Check runtime/service connectivity with the read-only quota diagnostic before resuming.',
    'quota_parsing': 'Inspect the sanitized schema diagnostic and the pinned SDK/runtime compatibility.',
    'quota_unknown': 'Obtain a fresh account quota observation for the configured bucket before resuming.',
    'quota_reserve': 'Wait until the applicable quota has recovered above the configured reserve; do not switch buckets.',
    'quota_exhausted': 'Wait for the reported account limit to recover; no credits or resets will be consumed.',
    'quota_rpc_error': 'Inspect the RPC diagnostic and check the runtime/service; no inference was started.',
}


def error(code, message, **facts):
    return FactoryError(code, message, details={'method': METHOD, 'next_step': NEXT_STEPS[code], **facts})


def diagnose(exc):
    """Preserve the *kind* and protocol cause without serializing arbitrary errors."""
    if isinstance(exc, FactoryError) and exc.code in NEXT_STEPS:
        exc.details.setdefault('next_step', NEXT_STEPS[exc.code])
        exc.details.setdefault('method', METHOD)
        return exc
    from pydantic import ValidationError
    message = str(exc).lower()  # Classified in memory only; never log raw auth/network payloads.
    rpc = getattr(exc, 'code', None)
    facts = {'exception': type(exc).__name__, 'rpc_code': rpc if type(rpc) is int else None}
    if rpc == -32601:
        code, cause = 'quota_method_unavailable', 'App Server rejected the method (-32601)'
    elif rpc == -32602 or 'not initialized' in message or 'configuration' in message:
        code, cause = 'quota_configuration', 'App Server rejected quota parameters or initialization/configuration'
    elif isinstance(exc, (TimeoutError,)) or any(s in message for s in ('timed out', 'timeout')):
        code, cause = 'quota_timeout', 'Account quota request timed out'
    elif isinstance(exc, ValidationError) or rpc == -32700 or any(s in message for s in ('response must be a json object', 'invalid json')):
        code, cause = 'quota_parsing', 'Account quota response does not match the documented schema'
    elif any(s in message for s in ('unauthorized', '401', '403', 'not authenticated', 'authentication', 'chatgpt auth', 'api key', 'api-key', 'invalid_grant', 'login')):
        code, cause = 'authentication', 'Account quota rejected authentication or protocol configuration'
    elif '429' in message or any(s in message for s in ('usage limit reached', 'quota exhausted', 'rate limit exceeded')):
        code, cause = 'quota_exhausted', 'Account quota service reported an exhausted limit'
    elif isinstance(exc, (ConnectionError, OSError)) or any(s in message for s in ('error sending request', 'connection', 'dns', 'certificate', 'tls', 'network', 'broken pipe', 'process exited', 'transport')):
        code, cause = 'quota_transport', 'Account quota transport failed'
        if 'failed to fetch codex rate limits: error sending request' in message:
            cause = 'failed to fetch codex rate limits: error sending request'
    elif re.search(r'\b50[0234]\b', message) or 'unavailable' in message or 'overloaded' in message:
        code, cause = 'quota_service', 'Account quota service is unavailable'
    else:
        code, cause = 'quota_rpc_error', 'Unclassified account quota failure; exception type and RPC code retained'
    return error(code, cause, **facts)


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def normalize(value, *, observed_at=None, method=METHOD):
    """Normalize single/multi-bucket responses. No max-free-bucket selection.

    The historical single view is Codex's default meter. An explicit limitId
    remains authoritative, and the keyed view takes precedence when supplied.
    Optional missing windows/reset times remain absent, never invented.
    """
    if not isinstance(value, dict):
        raise error('quota_parsing', 'Account quota must be a JSON object')
    buckets = value.get('rateLimitsByLimitId')
    if buckets is not None and not isinstance(buckets, dict):
        raise error('quota_parsing', 'rateLimitsByLimitId must be an object or null')
    buckets = dict(buckets or {})
    single = value.get('rateLimits')
    if single is not None:
        if not isinstance(single, dict):
            raise error('quota_parsing', 'rateLimits must be an object')
        buckets.setdefault(single.get('limitId') or 'codex', single)
    clean = {}
    for key, bucket in buckets.items():
        if not isinstance(key, str) or not isinstance(bucket, dict) or bucket.get('limitId') not in (None, key):
            raise error('quota_parsing', 'Quota bucket identity is malformed or inconsistent')
        # Keep only quota fields. Credits/reset-credit offers cannot authorize work.
        b = {k: bucket.get(k) for k in ('limitId', 'limitName', 'planType', 'rateLimitReachedType', 'spendControlReached')}
        for flag in ('spendControlReached',):
            if b[flag] is not None and type(b[flag]) is not bool:
                raise error('quota_parsing', 'Quota limit flag is malformed', field=flag)
        for name in ('primary', 'secondary'):
            w = bucket.get(name)
            if w is not None:
                if not isinstance(w, dict):
                    raise error('quota_parsing', 'Quota window must be an object or null', field=name)
                w = {k: w.get(k) for k in ('usedPercent', 'resetsAt', 'windowDurationMins')}
                for k, v in w.items():
                    if v is not None and (not number(v) or v < 0 or (k == 'usedPercent' and v > 100)):
                        raise error('quota_parsing', 'Quota window has an invalid numeric value', field=name + '.' + k)
            b[name] = w
        clean[key] = b
    return {'observed_at': time.time() if observed_at is None else observed_at,
            'buckets': clean, 'scope': 'shared_codex_account', 'method': method}


def quota_guard(observation, policy, now=None):
    now = time.time() if now is None else now
    observed = observation.get('observed_at') if isinstance(observation, dict) else None
    if not number(observed) or not 0 <= now - observed <= policy['quota_max_age_seconds']:
        raise error('quota_unknown', 'Quota telemetry is missing, stale or has a future timestamp')
    buckets = observation.get('buckets')
    bucket = buckets.get(policy['quota_bucket']) if isinstance(buckets, dict) else None
    if not isinstance(bucket, dict):
        raise error('quota_unknown', 'Configured quota bucket is unavailable', bucket=policy['quota_bucket'])
    if bucket.get('rateLimitReachedType') or bucket.get('spendControlReached'):
        raise error('quota_exhausted', 'The shared Codex account has reached a usage limit')
    windows = [bucket[k] for k in ('primary', 'secondary') if bucket.get(k) is not None]
    if not windows:
        raise error('quota_unknown', 'No measured quota window is available')
    for window in windows:
        if not isinstance(window, dict):
            raise error('quota_parsing', 'Quota window must be an object')
        used, reset = window.get('usedPercent'), window.get('resetsAt')
        if not number(used) or not 0 <= used <= 100:
            raise error('quota_unknown', 'Quota window has no valid measured usage')
        if reset is not None and (not number(reset) or reset <= now):
            raise error('quota_unknown', 'Quota window reset is invalid or expired; refresh telemetry')
        if used == 100:
            raise error('quota_exhausted', 'The shared Codex account has exhausted a measured window')
        if 100 - used <= policy['quota_reserve_percent']:
            raise error('quota_reserve', 'Shared Codex quota has reached the configured reserve',
                        bucket=policy['quota_bucket'], used_percent=used,
                        reserve_percent=policy['quota_reserve_percent'], resets_at=reset)
