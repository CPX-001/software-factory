"""SDK adapter tests are simulated; none sends a model request or uses real auth."""
from pathlib import Path
from types import SimpleNamespace as NS
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from openai_codex.errors import InternalRpcError

from factory.codex_execution import CodexExecution, DISABLED_FEATURES
from factory.execution_contract import DEFAULT_POLICY
from factory.execution_sandbox import LinuxSandbox
from factory.registry import FactoryError


class CodexExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / 'auth.json').write_text('{}')
        (root / 'product').mkdir()
        self.env = patch.dict('os.environ', {'CODEX_HOME': str(root), 'OPENAI_API_KEY': 'must-not-inherit'})
        self.env.start(); self.addCleanup(self.env.stop)
        self.codex = MagicMock()
        self.codex.account.return_value.account = NS(root=NS(type='chatgpt'))
        self.codex.models.return_value.data = [NS(model='offered-model', supported_reasoning_efforts=[NS(reasoning_effort=NS(value='low'))])]
        self.constructor = patch('openai_codex.Codex', return_value=self.codex)
        self.constructor.start(); self.addCleanup(self.constructor.stop)
        self.sandbox = LinuxSandbox(root / 'sandbox')
        self.worktree = root / 'product'
        self.policy = {**DEFAULT_POLICY, 'model': 'offered-model', 'effort': 'low'}

    def adapter(self):
        return CodexExecution(self.sandbox, self.policy, self.worktree)

    def test_effective_api_key_login_is_rejected(self):
        self.codex.account.return_value.account.root.type = 'apiKey'
        with self.assertRaises(FactoryError) as error:
            self.adapter()
        self.assertEqual(error.exception.code, 'authentication')
        self.codex.thread_start.assert_not_called()
        self.codex.close.assert_called()

    def test_model_and_effort_must_be_offered_by_installed_runtime(self):
        self.policy['effort'] = 'imagined-effort'
        with self.assertRaises(FactoryError) as error:
            self.adapter()
        self.assertEqual(error.exception.code, 'model_unavailable')

    def test_separate_bucket_cannot_authorize_a_standard_model(self):
        self.policy['quota_bucket'] = 'codex_bengalfox'
        with self.assertRaises(FactoryError) as error:
            self.adapter()
        self.assertEqual(error.exception.code, 'quota_meter_unsupported')
        self.codex.thread_start.assert_not_called()

    def test_isolated_configuration_disables_factory_tools_and_other_models(self):
        adapter = self.adapter()
        config = (adapter.home / 'config.toml').read_text()
        self.assertIn('forced_login_method="chatgpt"', config)
        self.assertIn('mcp_servers.software_factory={command="false",enabled=false}', config)
        for feature in DISABLED_FEATURES:
            self.assertIn(feature + '=false', config)
        self.assertNotIn('must-not-inherit', config)

    def test_quota_uses_official_typed_sdk_request_without_a_turn(self):
        adapter = self.adapter()
        self.codex._client.request.return_value.model_dump.return_value = {
            'rateLimitsByLimitId': {'codex': {'primary': {'usedPercent': 40, 'resetsAt': time.time()+1000}}}}
        value = adapter.quota()
        self.assertEqual(value['buckets']['codex']['primary']['usedPercent'], 40)
        self.assertEqual(self.codex._client.request.call_args.args[0], 'account/rateLimits/read')
        self.codex.thread_start.assert_not_called()

    def test_sdk_conversion_accepts_bucket_only_but_rejects_coercion(self):
        from factory.quota import quota_guard
        adapter = self.adapter()
        payload = {'rateLimitsByLimitId': {'codex': {'primary': {'usedPercent': 12}}}}
        self.codex._client.request.side_effect = lambda method, params, response_model: response_model.model_validate(payload)
        quota_guard(adapter.quota(), self.policy)
        for invalid in (True, '12'):
            payload = {'rateLimits': {'primary': {'usedPercent': invalid}}}
            with self.assertRaises(FactoryError) as exc:
                adapter.quota()
            self.assertEqual(exc.exception.code, 'quota_parsing')
        payload = {'rateLimits': {'primary': {}}}
        with self.assertRaises(FactoryError) as exc:
            quota_guard(adapter.quota(), self.policy)
        self.assertEqual(exc.exception.code, 'quota_unknown')
        self.codex.thread_start.assert_not_called()

    def test_recover_reads_exact_turn_without_starting_a_model(self):
        adapter = self.adapter()
        self.codex._client.thread_read.return_value.thread.turns = [NS(id='saved-turn',
            status=NS(value='completed'), items=[NS(root=NS(type='agentMessage', text='{"summary":"saved"}'))])]
        outcome = adapter.recover({'thread_id': 'saved-thread', 'turn_id': 'saved-turn'})
        self.assertEqual(outcome['response'], {'summary': 'saved'})
        self.codex.thread_start.assert_not_called()
        self.codex.thread_resume.assert_not_called()

    def test_pause_uses_native_interrupt_before_reporting_paused(self):
        adapter = self.adapter()
        handle = MagicMock()
        handle.id = 'turn-id'
        thread = self.codex.thread_start.return_value
        thread.id = 'thread-id'
        thread.turn.return_value = handle
        def stream():
            deadline = time.monotonic() + 2
            while not handle.interrupt.called and time.monotonic() < deadline:
                time.sleep(.02)
            yield NS(method='turn/completed', payload=NS(turn=NS(status=NS(value='interrupted'), error=None)))
        handle.stream.side_effect = stream
        with self.assertRaises(FactoryError) as error:
            adapter.respond({}, thread_id=None, should_stop=lambda: 'paused',
                on_runtime=lambda r: None, on_usage=lambda u: None, on_quota=lambda q: None)
        self.assertEqual(error.exception.code, 'paused')
        handle.interrupt.assert_called_once()

    def test_transport_failure_does_not_start_turn_or_login_fallback(self):
        adapter = self.adapter()
        self.codex._client.request.side_effect = InternalRpcError(-32603, 'failed to fetch codex rate limits: error sending request')
        with self.assertRaises(FactoryError) as exc:
            adapter.quota()
        self.assertEqual(exc.exception.code, 'quota_transport')
        self.assertEqual(exc.exception.details['rpc_code'], -32603)
        self.codex.thread_start.assert_not_called()
        self.codex.login_api_key.assert_not_called()
        self.assertEqual(self.codex._client.request.call_args.args[0], 'account/rateLimits/read')

    def test_actual_bounded_request_timeout_is_classified_and_closes_runtime(self):
        adapter = self.adapter()
        with self.assertRaises(FactoryError) as exc:
            adapter._bounded_request(lambda: time.sleep(.1), seconds=.01, failure='quota_timeout')
        self.assertEqual(exc.exception.code, 'quota_timeout')
        self.codex.close.assert_called()
