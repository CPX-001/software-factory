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
