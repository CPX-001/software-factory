from copy import deepcopy
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, MagicMock

from factory.__main__ import main
from factory.application import FactoryService
from factory.discovery import Discovery
from factory.registry import FactoryError, Registry
from factory.runtime import Runtime
from factory.skill_catalog import Catalog
from factory.skill_router import SkillRouter
from factory.workflow import Store
from tests.discovery_fakes import FakeModel, reply, complete_reply
from tests.architecture_fakes import FakeArchitect, proposal, review
from tests.planning_fakes import fake as fake_planner


class AuthorizedAnalysisTests(unittest.TestCase):
    def setUp(self):
        from scripts.execution_smoke_fixture import prepare_from_discovery
        from tests.execution_fakes import FakeSDK
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.prepared = prepare_from_discovery(root / 'pilot', 'gpt-5.6-terra', 'low', registry_home=root / 'registry')
        self.policy = deepcopy(self.prepared['policy'])
        self.policy['continuation'].update(max_calls=20, max_seconds=1800, max_tokens=250000)
        self.policy['quota_reserve_percent'] = 0
        self.verification = {'schema_version': 1, 'checks': self.prepared['contract']['checks'], 'harness': []}
        self.jobs = []
        self.sdk = FakeSDK(complete_reply(), lambda c: proposal(c['source']['knowledge']), review(),
                           lambda c: __import__('tests.planning_fakes', fromlist=['dynamic']).dynamic(c), review())
        self.service = FactoryService(Registry(root / 'registry'), analysis_worker_factory=self.sdk,
            router=SkillRouter(Catalog()), launcher=lambda p, r: self.jobs.append((p, r)))
        self.identifier = self.prepared['project']['id']
        self.store = self.service._store(self.identifier)

    def start(self):
        self.service.configure_execution(self.policy, self.verification, self.identifier)
        self.service.submit_user_message(self.prepared['initial_message'], self.identifier, request_id='analysis-once')
        self.service.resume(self.identifier)

    def test_real_phase_dispatch_uses_one_ledger_and_resume_cannot_reset_it(self):
        self.policy['continuation']['max_calls'] = 2
        self.start()
        with self.assertRaises(FactoryError) as exc:
            self.service.run_pending(*self.jobs[-1])
        self.assertEqual(exc.exception.code, 'budget_exhausted')
        status = self.service.get_status(self.identifier)
        group = status['continuation']
        self.assertEqual(group['budget']['analysis_calls'], 2)
        self.assertEqual(group['budget']['tokens'], 200)
        run = status['autonomous_run']['run_id']
        self.service.resume(self.identifier)
        self.assertEqual(self.jobs[-1][1], run)
        with self.assertRaises(FactoryError):
            self.service.run_pending(*self.jobs[-1])
        after = self.service.get_status(self.identifier)['continuation']
        self.assertEqual(after['id'], group['id'])
        self.assertEqual(after['deadline'], group['deadline'])
        self.assertEqual(after['budget']['calls'], 2)
        self.assertEqual(len(self.sdk.contexts), 2)

    def test_completed_analysis_output_is_reused_without_spending(self):
        from factory.analysis_execution import AnalysisModel, ensure_group
        self.start()
        ensure_group(self.store, self.jobs[-1][1])
        adapter = AnalysisModel(self.service, self.store, 'discovery')
        first = adapter.respond({'input': 'saved context'})
        second = AnalysisModel(self.service, self.store, 'discovery').respond({'input': 'saved context'})
        self.assertEqual(first, second)
        self.assertEqual(len(self.sdk.contexts), 1)
        self.assertEqual(self.service.get_status(self.identifier)['continuation']['budget']['calls'], 1)
        self.assertEqual(self.sdk.contexts[0]['workflow_authorization']['quota_reserve_percent'], 0)
        self.assertEqual(self.sdk.contexts[0]['workflow_authorization']['continuation']['max_calls'], 20)

    def test_all_analysis_phases_stop_at_binding_boundary_and_keep_budget(self):
        self.start()
        self.service.run_pending(*self.jobs[-1])
        status = self.service.get_status(self.identifier)
        self.assertEqual(status['phase'], 'execution')
        self.assertEqual(status['continuation']['state'], 'implementation_boundary')
        self.assertEqual(status['continuation']['budget']['calls'], 5)
        self.assertEqual(status['continuation']['budget']['analysis_tokens'], 500)
        self.assertEqual(status['execution']['diagnostic']['code'], 'verification_binding_pending')
        from factory.execution_store import ExecutionStore
        self.assertEqual(ExecutionStore(self.store).acceptances(), [])
        self.service.resume(self.identifier)
        self.assertEqual(len(self.jobs), 1)

    def test_explicit_zero_reserve_allows_measured_usage_but_never_exhaustion(self):
        self.sdk.quota_value = {'observed_at': time.time(), 'buckets': {'codex': {'primary': {
            'usedPercent': 99, 'resetsAt': time.time() + 1000}}}}
        self.policy['continuation']['max_calls'] = 1
        self.start()
        with self.assertRaises(FactoryError) as exc:
            self.service.run_pending(*self.jobs[-1])
        self.assertEqual(exc.exception.code, 'budget_exhausted')
        self.assertEqual(len(self.sdk.contexts), 1)
        from factory.quota import quota_guard
        self.sdk.quota_value['buckets']['codex']['primary']['usedPercent'] = 100
        with self.assertRaises(FactoryError) as exc:
            quota_guard(self.sdk.quota_value, self.policy)
        self.assertEqual(exc.exception.code, 'quota_exhausted')

    def test_plan_binding_preserves_predeclared_checks_and_all_consumed_budget(self):
        self.start()
        self.service.run_pending(*self.jobs[-1])
        before = self.service.get_status(self.identifier)['continuation']
        definition = deepcopy(self.verification)
        for check in definition['checks']:
            check.update(gate='milestone_gate', criteria=[0, 1])
        local = deepcopy(definition['checks'][0])
        local.update(id='local_summary', gate='local_gate', criteria=[0])
        definition['checks'].append(local)
        weakened = deepcopy(definition)
        weakened['checks'][0]['min_tests'] = 1
        with self.assertRaises(FactoryError) as exc:
            self.service.configure_execution(self.policy, weakened, self.identifier)
        self.assertEqual(exc.exception.code, 'acceptance_contract_frozen')
        self.service.configure_execution(self.policy, definition, self.identifier)
        status = self.service.execute_next_slice(self.identifier, request_id='same-workflow-execution')
        after = status['continuation']
        self.assertEqual(before['id'], after['id'])
        self.assertEqual(before['runtime_id'], after['runtime_id'])
        self.assertEqual(before['deadline'], after['deadline'])
        self.assertEqual(before['budget']['calls'], after['budget']['calls'])
        self.assertEqual(before['budget']['tokens'], after['budget']['tokens'])

    def test_missing_usage_blocks_the_next_phase_without_an_extra_inference(self):
        self.sdk.usage = None
        self.start()
        with self.assertRaises(FactoryError) as exc:
            self.service.run_pending(*self.jobs[-1])
        self.assertEqual(exc.exception.code, 'usage_unknown')
        self.assertEqual(len(self.sdk.contexts), 1)

    def test_one_planning_recovery_retains_history_and_cannot_repeat(self):
        from tests.planning_fakes import dynamic
        finding = {'id': 'closure_cycle', 'severity': 'high', 'category': 'verification',
                   'targets': ['milestone_gate'], 'description': 'Closure ordering needs correction',
                   'recommendation': 'Preserve criteria and repair the ordering'}
        self.sdk.responses = [complete_reply(), lambda c: proposal(c['source']['knowledge']), review(),
                              dynamic, review([finding]), dynamic, review([finding]), dynamic, review([finding])]
        self.start()
        self.service.run_pending(*self.jobs[-1])
        state = self.store.snapshot()['planning']
        self.assertEqual(state['stage'], 'blocked')
        self.assertEqual(state['calls'], 6)
        self.assertEqual(state['recovery_attempts'], 1)
        before = self.service.get_status(self.identifier)['continuation']['budget']['calls']
        self.service.resume(self.identifier)
        self.assertEqual(self.service.get_status(self.identifier)['continuation']['budget']['calls'], before)
        self.assertEqual(len(self.jobs), 1)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.registry = Registry(self.root / 'state')
        self.registry.allow_root(self.root / 'projects' if (self.root / 'projects').exists() else self.root)
        self.jobs = []
        self.discovery = FakeModel(reply())
        self.architect = FakeArchitect(proposal(), review())
        self.service = FactoryService(self.registry, discovery_model=self.discovery, architecture_model=self.architect,
                                      planning_model=fake_planner(), router=SkillRouter(Catalog()), launcher=lambda p, r: self.jobs.append((p, r)))
        self.project = self.service.initialize_project(str(self.root / 'projects' / 'first'))['project']
        self.store = Store(self.project['path'])

    def drive(self):
        self.service.run_pending(*self.jobs[-1])

    def test_initialization_selection_and_compact_status_independent_of_cli(self):
        status = self.service.get_status()
        self.assertEqual(status['project']['id'], self.project['id'])
        self.assertEqual(status['phase'], 'discovery')
        self.assertEqual(status['autonomous_run']['status'], 'idle')
        self.assertEqual(self.jobs, [])
        self.assertNotIn('knowledge', status)
        self.assertLess(len(json.dumps(status)), 4000)
        self.assertEqual(FactoryService(Registry(self.registry.home)).get_status(), status)

    def test_message_is_durable_before_worker_and_continuation_crosses_phases(self):
        self.discovery.responses = [complete_reply()]
        status = self.service.submit_user_message('My product', request_id='message-1')
        self.assertEqual(status['autonomous_run']['status'], 'queued')
        self.assertEqual(len(self.discovery.contexts), 0)
        self.assertIsNotNone(self.store.snapshot()['discovery']['pending_turn'])
        self.drive()
        status = self.service.get_status()
        self.assertEqual(status['phase'], 'execution')
        self.assertEqual(status['autonomous_run']['status'], 'implementation_boundary')
        self.assertEqual(status['architecture_revision']['revision'], 1)
        self.assertEqual(len(self.architect.contexts), 2)
        self.service.resume()
        self.assertEqual(len(self.jobs), 1)
        self.service.run_pending(*self.jobs[0])
        self.assertEqual(len(self.discovery.contexts), 1)

    def test_discovery_pending_decision_and_answer_automatically_continue(self):
        decision = {'key': 'scope', 'question': 'Public or private?', 'why': 'Access boundary', 'recommendation': 'Public'}
        self.discovery.responses = [reply(decisions=[decision]), complete_reply()]
        self.service.submit_user_message('Initial idea')
        self.drive()
        decisions = self.service.list_pending_decisions()
        self.assertEqual(decisions['total'], 1)
        self.assertEqual(decisions['items'][0]['recommendation'], 'Public')
        self.service.answer_decision(decisions['items'][0]['id'], 'Public')
        self.assertEqual(len(self.jobs), 2)
        final = complete_reply()
        final['resolve_questions'] = [{'key': 'audience', 'reason': 'Known from the answered scope brief'}]
        self.discovery.responses = [final]
        # Human decision records are included in architectural coverage.
        facts = final['knowledge'] + [i for i in self.store.snapshot()['discovery']['knowledge'] if i['key'].startswith('human_decision_')]
        self.architect.responses = [proposal(facts), review()]
        self.drive()
        self.assertEqual(self.service.get_status()['phase'], 'execution')

    def test_pause_survives_reconnection_messages_are_saved_without_starting(self):
        self.service.pause()
        self.service.submit_user_message('Saved during pause')
        self.assertFalse(self.jobs)
        reconnected = FactoryService(Registry(self.registry.home), launcher=lambda p, r: self.jobs.append((p, r)))
        self.assertEqual(reconnected.get_status()['state'], 'paused')
        self.assertEqual(reconnected.get_next_action()['action'], 'resume')
        reconnected.resume()
        self.assertEqual(len(self.jobs), 1)
        self.drive()
        self.assertEqual(self.service.get_status()['autonomous_run']['status'], 'waiting_for_input')

    def test_pause_in_flight_saves_response_and_prevents_architecture_call(self):
        entered, release = threading.Event(), threading.Event()
        class Slow:
            def respond(inner, context):
                entered.set(); release.wait(5)
                return complete_reply()
        self.service.discovery_model = Slow()
        self.service.submit_user_message('Start')
        worker = threading.Thread(target=self.drive)
        worker.start()
        self.assertTrue(entered.wait(3))
        status = self.service.pause()
        self.assertTrue(status['autonomous_run']['paused'])
        release.set(); worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.store.snapshot()['phase'], 'architecture')
        self.assertEqual(self.architect.contexts, [])
        self.assertEqual(self.service.get_status()['autonomous_run']['status'], 'paused')
        self.service.resume(); self.drive()
        self.assertEqual(self.service.get_status()['phase'], 'execution')

    def test_pause_between_architecture_calls(self):
        self.discovery.responses = [complete_reply()]
        def pausing(_):
            self.service.pause()
            return proposal()
        self.architect.responses = [pausing]
        self.service.submit_user_message('Start'); self.drive()
        self.assertEqual(self.service.get_status()['autonomous_run']['status'], 'paused')
        self.assertEqual(len(self.architect.contexts), 1)
        self.architect.responses = [review()]
        self.service.resume(); self.drive()
        self.assertEqual(self.service.get_status()['phase'], 'execution')
        self.assertEqual([c['role'] for c in self.architect.contexts], ['propose', 'critic'])

    def test_registered_allowlist_symlinks_and_ambiguous_selection(self):
        with tempfile.TemporaryDirectory() as outside:
            with self.assertRaisesRegex(FactoryError, 'outside'):
                self.service.initialize_project(outside)
            link = self.root / 'escape'; link.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(FactoryError):
                self.service.initialize_project(str(link))
        self.service.initialize_project(str(self.root / 'projects/second'))
        with self.registry.connection() as db:
            db.execute('DELETE FROM selections')
        with self.assertRaises(FactoryError) as error:
            self.service.get_status()
        self.assertEqual(error.exception.code, 'project_selection_required')
        self.service.select_project(self.project['id'])
        self.assertEqual(self.service.get_status()['project'], self.project)
        with self.assertRaises(FactoryError):
            self.service.get_status('/etc')

    def test_request_retry_does_not_duplicate_discovery_after_completion(self):
        self.service.submit_user_message('Same', request_id='retry')
        self.drive()
        self.service.submit_user_message('Same', request_id='retry')
        self.assertEqual(len(self.jobs), 1)
        with self.store._connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM discovery_turns').fetchone()[0], 1)
        with self.assertRaisesRegex(ValueError, 'different input'):
            self.service.submit_user_message('Different', request_id='retry')

    def test_worker_failure_requires_explicit_retry_no_loop(self):
        self.discovery.responses = [ValueError('Invalid model output'), reply()]
        self.service.submit_user_message('Start')
        with self.assertRaises(ValueError):
            self.drive()
        self.assertEqual(self.service.get_status()['state'], 'failed')
        self.assertEqual(len(self.jobs), 1)
        self.service.resume(); self.drive()
        self.assertEqual(self.service.get_status()['autonomous_run']['status'], 'waiting_for_input')

    def test_queued_and_running_resumes_are_idempotent(self):
        self.service.submit_user_message('Start')
        self.service.resume(); self.service.resume()
        self.assertEqual(len(self.jobs), 1)
        with self.assertRaisesRegex(FactoryError, 'starting'):
            self.service.submit_user_message('Another input')
        self.drive()
        self.service.resume()
        self.assertEqual(len(self.jobs), 1)

    def test_stale_run_and_crash_recovery_keep_state(self):
        self.service.submit_user_message('Start')
        runtime = Runtime(self.store)
        old = self.jobs[0]
        runtime.update(old[1], 'running')  # Simulate a process dying without releasing state.
        self.assertEqual(self.service.get_status()['state'], 'interrupted')
        self.service.resume()
        self.assertEqual(len(self.jobs), 2)
        self.service.run_pending(*old)  # Superseded process cannot run the new job.
        self.assertFalse(self.discovery.contexts)
        self.drive()
        self.assertEqual(len(self.discovery.contexts), 1)

    def test_architecture_inspection_preserves_baseline_fingerprint(self):
        self.discovery.responses = [complete_reply()]
        self.service.submit_user_message('Start'); self.drive()
        before = self.store.events()
        baseline = self.service.get_architecture()
        self.assertEqual(baseline['revision'], 1)
        self.assertTrue(self.service.get_architecture(view='adrs'))
        self.assertEqual(self.service.get_architecture(view='revision')['fingerprint'], baseline['fingerprint'])
        self.assertEqual(before, self.store.events())

    def test_cli_uses_application_service_for_existing_commands(self):
        fake = MagicMock()
        fake.get_architecture.return_value = {'revision': 4}
        with patch('factory.__main__.FactoryService.for_local', return_value=fake), \
             patch.object(sys, 'argv', ['factory', 'architecture-show', self.project['path']]), \
             patch('sys.stdout', new=io.StringIO()) as output:
            main()
        fake.get_architecture.assert_called_once_with()
        self.assertEqual(json.loads(output.getvalue()), {'revision': 4})

    def test_internal_workers_disable_only_the_factory_interface(self):
        from factory.codex_worker import worker_overrides
        config = self.root / 'codex'; config.mkdir()
        (config / 'config.toml').write_text("""
[plugins."software-factory@personal"]
enabled = true
[plugins."other@personal"]
enabled = true
[mcp_servers.factory_direct]
command = 'python3'
args = ['-m', 'factory.mcp_server']
""")
        with patch.dict(os.environ, {'CODEX_HOME': str(config)}):
            overrides = worker_overrides()
        self.assertIn('mcp_servers.software_factory={command="python3",args=["-m","factory.mcp_server"],enabled=false}', overrides)
        self.assertIn('plugins."software-factory@personal".enabled=false', overrides)
        self.assertIn('mcp_servers."factory_direct".enabled=false', overrides)
        self.assertNotIn('other', str(overrides))

    def test_launcher_is_fixed_detached_and_has_no_client_command(self):
        service = FactoryService(self.registry)
        with patch('factory.application.subprocess.Popen') as popen:
            service._launch(self.project['id'], 'run-id')
        args, kwargs = popen.call_args
        self.assertEqual(args[0][1:3], ['-m', 'factory.runner'])
        self.assertTrue(kwargs['start_new_session'])
        self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
        self.assertNotIn('shell', kwargs)


if __name__ == '__main__':
    unittest.main()
