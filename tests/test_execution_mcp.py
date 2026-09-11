"""Actual stdio + detached controller + simulated SDK + real isolated verification."""
import asyncio
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from factory.application import FactoryService
from factory.registry import Registry
from tests.execution_fakes import FakeSDK, fixture


class ExecutionMCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_detached_slice_survives_chat_close_repairs_and_checkpoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service, store, jobs, _ = fixture(root, FakeSDK(), harness_during=True)
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
from tests.execution_fakes import FakeSDK, result
sdk=FakeSDK(result(0, tests=True), result(3))
def wait(stop):
    Path({str(entered)!r}).touch()
    end=time.monotonic()+15
    while not Path({str(release)!r}).exists() and time.monotonic()<end:
        time.sleep(.05)
sdk.on_call=wait
FactoryService(Registry(sys.argv[1]), execution_worker_factory=sdk,
    router=SkillRouter(Catalog())).run_pending(sys.argv[2], sys.argv[3])
''')
            bootstrap = root / 'server_bootstrap.py'
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
                response = (await client.call_tool('factory_execute', {'request_id': 'one-slice'})).structured_content
                self.assertTrue(response['ok'], response)
                identifier = response['data']['execution']['id']
                for _ in range(100):
                    if entered.exists():
                        break
                    await asyncio.sleep(.05)
                self.assertTrue(entered.exists())
                retry = (await client.call_tool('factory_execute', {'request_id': 'one-slice'})).structured_content
                self.assertEqual(retry['data']['execution']['id'], identifier)
            # Neither the MCP connection nor this conversation supervises the work.
            release.touch()
            reader = FactoryService(Registry(service.registry.home))
            for _ in range(150):
                status = reader.get_status()
                if status['execution']['state'] == 'checkpoint':
                    break
                await asyncio.sleep(.1)
            self.assertEqual(status['execution']['state'], 'checkpoint', status)
            self.assertEqual(status['execution']['attempt'], 2)
            async with Client(parameters) as client:
                inspection = (await client.call_tool('factory_inspect', {'view': 'execution'})).structured_content['data']
                self.assertEqual(inspection['attempts'][0]['verification_status'], 'FAIL')
                self.assertEqual(inspection['attempts'][1]['verification_status'], 'PASS')
                self.assertEqual(inspection['id'], identifier)
                self.assertEqual(reader.snapshot()['phase'], 'execution')
