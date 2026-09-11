import asyncio
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from factory.application import FactoryService
from factory.discovery import Discovery
from factory.mcp_server import FactoryTools, TOOLS, build_server
from factory.registry import Registry
from factory.runtime import Runtime
from factory.skill_catalog import Catalog
from factory.skill_router import SkillRouter
from factory.workflow import Store
from tests.discovery_fakes import FakeModel, reply, complete_reply
from tests.architecture_fakes import FakeArchitect, proposal, review
from tests.planning_fakes import fake as fake_planner


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.registry = Registry(self.root / 'registry')
        self.registry.allow_root(self.root)
        self.jobs = []
        self.service = FactoryService(self.registry, workflow_mode='verified', discovery_model=FakeModel(reply()),
            architecture_model=FakeArchitect(proposal(), review()), planning_model=fake_planner(), router=SkillRouter(Catalog()),
            launcher=lambda p, r: self.jobs.append((p, r)))
        self.tools = FactoryTools(self.service)

    def init(self):
        result = self.tools.call('factory_project', {'action': 'init', 'path': str(self.root / 'project')})
        self.assertTrue(result['ok'], result)
        return result['data']['project']['id']

    def test_mcp_adapter_calls_same_application_service(self):
        service = MagicMock()
        service.get_status.return_value = {'phase': 'discovery'}
        result = FactoryTools(service).call('factory_status', {})
        self.assertEqual(result, {'ok': True, 'data': {'phase': 'discovery'}})
        service.get_status.assert_called_once_with()

    def test_tools_list_init_status_message_pause_and_resume(self):
        project = self.init()
        self.assertEqual(self.tools.call('factory_project', {'action': 'list'})['data']['total'], 1)
        self.assertTrue(self.tools.call('factory_pause', {})['data']['autonomous_run']['paused'])
        status = self.tools.call('factory_message', {'message': 'I want a product', 'request_id': 'one'})
        self.assertEqual(status['data']['state'], 'paused')
        self.assertFalse(self.jobs)
        resumed = self.tools.call('factory_resume', {})
        self.assertEqual(resumed['data']['autonomous_run']['status'], 'queued')
        self.service.run_pending(*self.jobs[0])
        self.assertEqual(self.tools.call('factory_status', {})['data']['autonomous_run']['status'], 'waiting_for_input')
        self.assertEqual(resumed['data']['project']['id'], project)

    def test_pending_decision_answer_and_inspection(self):
        project = self.init()
        store = Store(self.registry.resolve(project)['path'])
        decision_id = store.request_decision('Public repositories only?', store.snapshot()['revision'])
        self.tools.call('factory_pause', {})
        decisions = self.tools.call('factory_decisions', {})
        self.assertEqual(decisions['data']['items'][0]['id'], decision_id)
        answer = self.tools.call('factory_answer', {'decision_id': decision_id, 'answer': 'Yes, public only'})
        self.assertTrue(answer['ok'])
        self.assertEqual(answer['data']['pending_decisions']['count'], 0)
        self.assertFalse(self.jobs)
        discovery = self.tools.call('factory_inspect', {'view': 'discovery'})['data']
        self.assertTrue(any('public only' in i['text'] for i in discovery['knowledge']))
        self.assertIsNone(self.tools.call('factory_inspect', {'view': 'revision'})['data'])

    def test_closed_tools_cannot_execute_sql_commands_or_skip_gates(self):
        self.init()
        for name, args in (
            ('factory_transition', {'phase': 'planning'}),
            ('factory_status', {'sql': 'DROP TABLE workflow'}),
            ('factory_resume', {'phase': 'planning', 'completed': True}),
            ('factory_message', {'message': 'Hi', 'command': 'touch /tmp/injected'}),
            ('factory_project', {'action': 'init', 'path': '/etc'}),
            ('factory_project', {'action': 'list', 'path': '/etc'}),
            ('factory_inspect', {'view': 'architecture', 'revision': 100}),
            ('factory_status', {'project': '/etc'}),
            ('factory_decisions', {'limit': True}),
            ('factory_message', {'message': 'a' * 8001}),
            ('factory_answer', {'decision_id': True, 'answer': 'approve'}),
        ):
            with self.subTest(name=name, args=args):
                result = self.tools.call(name, args)
                self.assertFalse(result['ok'], result)
                self.assertIn('code', result['error'])
        self.assertEqual(self.service.get_status()['phase'], 'discovery')
        self.assertFalse(self.jobs)
        self.assertEqual(len(TOOLS), 10)

    def test_ambiguous_missing_and_unregistered_projects_are_structured(self):
        self.assertEqual(self.tools.call('factory_status', {})['error']['code'], 'project_selection_required')
        first = self.init()
        second = self.tools.call('factory_project', {'action': 'init', 'path': str(self.root / 'second')})['data']['project']['id']
        with self.registry.connection() as db:
            db.execute('DELETE FROM selections')
        result = self.tools.call('factory_status', {})
        self.assertEqual(result['error']['code'], 'project_selection_required')
        self.tools.call('factory_project', {'action': 'select', 'project': first})
        self.assertEqual(self.tools.call('factory_status', {})['data']['project']['id'], first)
        self.assertEqual(self.tools.call('factory_status', {'project': second})['data']['project']['id'], second)
        self.assertEqual(self.tools.call('factory_status', {'project': 'p_0000000000000000'})['error']['code'], 'project_not_registered')

    def test_status_stays_compact_with_large_architecture_history(self):
        project = self.init()
        self.service.discovery_model = FakeModel(complete_reply())
        self.service.submit_user_message('Complete brief'); self.service.run_pending(*self.jobs[0])
        status = self.tools.call('factory_status', {})
        architecture = self.tools.call('factory_inspect', {'view': 'architecture'})
        self.assertLess(len(json.dumps(status)), 5000)
        self.assertNotIn('components', json.dumps(status))
        self.assertEqual(architecture['data']['revision'], 1)
        self.assertTrue(self.tools.call('factory_inspect', {'view': 'adrs'})['data'])
        self.assertEqual(self.tools.call('factory_inspect', {'view': 'revision'})['data'], status['data']['architecture_revision'])


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovery_pilot_reconnects_without_model_or_budget_bypass(self):
        from scripts.execution_smoke_fixture import prepare_from_discovery
        from scripts.smoke_continuation import discovery_smoke
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            prepared = prepare_from_discovery(root / 'pilot', 'gpt-5.6-terra', 'low', registry_home=root / 'registry')
            jobs = []
            service = FactoryService(Registry(prepared['registry_home']), launcher=lambda p, r: jobs.append((p, r)))
            with patch('scripts.diagnose_execution.inspect_runtime', return_value={'status': 'ready', 'model_calls': 0}):
                a = await discovery_smoke(prepared, True, parameters=build_server(service))
                b = await discovery_smoke(prepared, True, parameters=build_server(service))
            self.assertEqual(a['blocker']['code'], 'workflow_budget_unavailable')
            self.assertEqual(a['project'], b['project'])
            self.assertEqual(a['run_id'], b['run_id'])
            self.assertTrue(a['selection_survived_reconnect'])
            self.assertEqual(a['model_calls_this_invocation'], 0)
            self.assertEqual(a['phases_really_completed'], [])
            self.assertEqual(a['independent_acceptance']['status'], 'NOT_RUN')
            self.assertEqual(jobs, [])
            store = Store(prepared['project']['path'])
            with store._connection() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM discovery_turns').fetchone()[0], 1)
                self.assertEqual(db.execute('SELECT count(*) FROM factory_runs').fetchone()[0], 0)

    async def test_official_sdk_negotiates_and_returns_structured_results(self):
        with tempfile.TemporaryDirectory() as path:
            service = FactoryService(Registry(path))
            async with Client(build_server(service)) as client:
                listing = await client.list_tools()
                self.assertEqual({t.name for t in listing.tools}, set(TOOLS))
                status = await client.call_tool('factory_status', {})
                self.assertTrue(status.is_error)
                self.assertFalse(status.structured_content['ok'])
                listing_result = await client.call_tool('factory_project', {'action': 'list'})
                self.assertEqual(listing_result.structured_content['data']['total'], 0)

    async def test_real_stdio_restart_keeps_selected_project_and_pause(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            registry = Registry(root / 'state'); registry.allow_root(root)
            parameters = StdioServerParameters(command=sys.executable,
                args=['-m', 'factory.mcp_server', '--home', str(registry.home)], cwd=str(Path(__file__).resolve().parent.parent))
            async with Client(parameters) as client:
                initialized = await client.call_tool('factory_project', {'action': 'init', 'path': str(root / 'project'), 'workflow': 'verified'})
                project = initialized.structured_content['data']['project']['id']
                await client.call_tool('factory_pause', {})
                queued = await client.call_tool('factory_message', {'message': 'Durable without a worker', 'request_id': 'persisted'})
                self.assertTrue(queued.structured_content['ok'])
                self.assertEqual(queued.structured_content['data']['autonomous_run']['status'], 'idle')
            async with Client(parameters) as client:
                status = (await client.call_tool('factory_status', {})).structured_content['data']
                self.assertEqual(status['project']['id'], project)
                self.assertEqual(status['state'], 'paused')
                self.assertIsNotNone(Store(root / 'project').snapshot()['discovery']['pending_turn'])
                bad = await client.call_tool('factory_resume', {'command': 'echo no'})
                self.assertTrue(bad.is_error)

    async def test_detached_worker_outlives_mcp_connection_without_real_codex(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            repo = Path(__file__).resolve().parent.parent
            registry = Registry(root / 'state'); registry.allow_root(root)
            started, release = root / 'started', root / 'release'
            worker = root / 'test-python'
            worker.write_text(f'''#!{sys.executable}
import sys,time
from pathlib import Path
sys.path.insert(0, {str(repo)!r})
from factory.application import FactoryService
from tests.discovery_fakes import complete_reply
from tests.architecture_fakes import FakeArchitect, proposal, review
from tests.planning_fakes import fake
from factory.skill_router import SkillRouter
from factory.skill_catalog import Catalog
original = FactoryService.__init__
class FakeWorker:
 def respond(self, context):
  Path({str(started)!r}).touch()
  deadline=time.monotonic()+10
  while not Path({str(release)!r}).exists() and time.monotonic()<deadline:
   time.sleep(0.05)
  return complete_reply()
def init(self,*a,**kw):
 kw['discovery_model']=FakeWorker()
 kw['architecture_model']=FakeArchitect(proposal(), review())
 kw['planning_model']=fake()
 kw['router']=SkillRouter(Catalog())
 original(self,*a,**kw)
FactoryService.__init__=init
sys.argv=['factory.runner',*sys.argv[3:]]
from factory.runner import main
main()
''')
            worker.chmod(0o700)
            bootstrap = root / 'server.py'
            bootstrap.write_text(f'''import sys
sys.path.insert(0, {str(repo)!r})
import factory.application
factory.application.sys.executable={str(worker)!r}
from factory.mcp_server import main
main()
''')
            parameters = StdioServerParameters(command=sys.executable,
                args=[str(bootstrap), '--home', str(registry.home)])
            async with Client(parameters) as client:
                await client.call_tool('factory_project', {'action': 'init', 'path': str(root / 'project'), 'workflow': 'verified'})
                response = await client.call_tool('factory_message', {'message': 'Mock worker only'})
                self.assertTrue(response.structured_content['ok'])
                for _ in range(100):
                    if started.exists():
                        break
                    await asyncio.sleep(0.05)
                self.assertTrue(started.exists())
            # MCP and its host connection have ended. Only the detached Factory process remains.
            release.touch()
            service = FactoryService(registry)
            for _ in range(100):
                if service.get_status()['autonomous_run']['status'] == 'implementation_boundary':
                    break
                await asyncio.sleep(0.05)
            self.assertEqual(service.get_status()['autonomous_run']['status'], 'implementation_boundary')
            self.assertIsNone(Store(root / 'project').snapshot()['discovery']['pending_turn'])

            self.assertEqual(service.get_status()['phase'], 'execution')
            async with Client(parameters) as client:
                response = await client.call_tool('factory_inspect', {'view': 'next_slice'})
                self.assertEqual(response.structured_content['data']['slice']['id'], 's1')
                strategy = await client.call_tool('factory_inspect', {'view': 'verification'})
                self.assertEqual(len(strategy.structured_content['data']['gates']), 2)
                roadmap = await client.call_tool('factory_inspect', {'view': 'plan'})
                self.assertTrue(roadmap.structured_content['data']['accepted'])
