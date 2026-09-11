"""Real MCP transports/processes, fake model, real isolated checks of three slices."""
import asyncio
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import Client
from mcp.client.stdio import StdioServerParameters
from factory.application import FactoryService
from factory.registry import Registry
from tests.continuation_fakes import setup


class ContinuationMCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnected_client_does_not_supervise_three_slice_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service, store, _, git, _, _ = setup(root, harness=True)
            repo = Path(__file__).resolve().parent.parent
            release, entered = root / 'release', root / 'entered'
            worker = root / 'worker.py'
            worker.write_text(f'''
import sys, time
from pathlib import Path
sys.path.insert(0, {str(repo)!r})
from factory.application import FactoryService
from factory.registry import Registry
from factory.skill_router import SkillRouter
from factory.skill_catalog import Catalog
from tests.execution_fakes import FakeSDK
from tests.continuation_fakes import implementation_a, implementation_b, implementation_c, refinement
sdk=FakeSDK(implementation_a(harness=True), implementation_b(), implementation_c())
def wait(stop):
    Path({str(entered)!r}).touch()
    end=time.monotonic()+15
    while not Path({str(release)!r}).exists() and time.monotonic()<end:
        time.sleep(.05)
sdk.on_call=wait
FactoryService(Registry(sys.argv[1]), execution_worker_factory=sdk,
    refinement_worker_factory=FakeSDK(refinement, refinement),
    router=SkillRouter(Catalog())).run_pending(sys.argv[2], sys.argv[3])
''')
            bootstrap = root / 'server.py'
            bootstrap.write_text(f'''
import sys, subprocess
sys.path.insert(0, {str(repo)!r})
from factory.application import FactoryService
def launch(self, project, run_id):
    subprocess.Popen([sys.executable, {str(worker)!r}, str(self.registry.home), project, run_id],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, close_fds=True)
FactoryService._launch=launch
from factory.mcp_server import main
main()
''')
            parameters = StdioServerParameters(command=sys.executable,
                args=[str(bootstrap), '--home', str(service.registry.home)])
            async with Client(parameters) as client:
                async def execute(request):
                    return (await client.call_tool('factory_execute', {'request_id': request})).structured_content
                a, b = await asyncio.gather(execute('pilot'), execute('pilot'))
                self.assertTrue(a['ok'], a); self.assertTrue(b['ok'], b)
                identifier = a['data']['requested_continuation_id']
                self.assertEqual(identifier, b['data']['requested_continuation_id'])
                c = await execute('second-client')
                self.assertEqual(c['data']['requested_continuation_id'], identifier)
                for _ in range(100):
                    if entered.exists():
                        break
                    await asyncio.sleep(.05)
                self.assertTrue(entered.exists())
            release.touch()  # Launch connection is closed before A can finish.
            reader = FactoryService(Registry(service.registry.home))
            for _ in range(300):
                status = reader.get_status()
                if status['continuation']['state'] == 'validation_pending':
                    break
                await asyncio.sleep(.1)
            self.assertEqual(status['state'], 'validation_pending', status)
            self.assertEqual(status['continuation']['budget']['calls'], 5)
            self.assertEqual([a['slice_id'] for a in status['continuation']['accepted']], ['s1', 's2', 's3'])
            async with Client(parameters) as client:
                duplicate = (await client.call_tool('factory_execute', {'request_id': 'pilot'})).structured_content['data']
                self.assertEqual(duplicate['requested_continuation_id'], identifier)
                detail = (await client.call_tool('factory_inspect', {'view': 'execution'})).structured_content['data']
                self.assertEqual(detail['continuation']['state'], 'validation_pending')
                refined = (await client.call_tool('factory_inspect', {'view': 'refinement', 'slice_id': 's2'})).structured_content
                self.assertTrue(refined['ok'], refined)
                self.assertEqual(len(refined['data']['execution_refinements']), 1)
            self.assertIn('from app import add', git('show', 'factory/accepted:report.py'))
            self.assertEqual(reader.snapshot()['phase'], 'execution')
