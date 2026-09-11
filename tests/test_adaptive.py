"""Adaptive workflow tests use disposable products and simulated Codex workers."""
from copy import deepcopy
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from factory.adaptive import Adaptive, enabled
from factory.application import FactoryService
from factory.codex_adaptive import CodexAdaptive
from factory.mcp_server import FactoryTools
from factory.registry import FactoryError, Registry
from factory.workflow import Store


def checkpoint(**changes):
    return {'focus': 'implementation', 'status': 'continue', 'message': 'Sigo con el alcance acordado.',
            'objective': 'Producto del usuario', 'memory': 'Decisión: usar HTML y JavaScript.',
            'next_step': 'Implementar la siguiente capacidad.', 'tasks': [], 'checks': [],
            'questions': [], 'documents': [], 'deferred': [], 'revisions': [], **changes}


class FakeCodex:
    def __init__(self, responses):
        self.responses = list(responses)
        self.contexts = []
        self.calls = 0
        self.recovered = None
        self.recoveries = []

    def __call__(self, project, settings):
        self.project = Path(project)
        self.settings = deepcopy(settings)
        return self

    def respond(self, context, **callbacks):
        self.calls += 1
        self.contexts.append(deepcopy(context))
        callbacks['on_runtime']({'thread_id': 'thread-' + str(self.calls), 'turn_id': 'turn-1', 'process': None})
        callbacks['on_usage']({'total': {'totalTokens': 100}})
        response = self.responses.pop(0)
        if callable(response):
            response = response(self, context, callbacks)
        if isinstance(response, BaseException):
            raise response
        return {'response': response, 'usage': {'total': {'totalTokens': 100}}, 'worker_status': 'completed'}

    def recover(self, runtime):
        self.recoveries.append(runtime)
        return self.recovered

    def close(self):
        pass


class AdaptiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.product = self.root / 'product'
        self.jobs = []
        self.worker = FakeCodex([])
        self.service = FactoryService(Registry(self.root / 'registry'), adaptive_worker_factory=self.worker,
                                      launcher=lambda p, r: self.jobs.append((p, r)))
        self.pid = self.service.initialize_project(str(self.product))['project']['id']
        self.store = Store(self.product)
        self.journal = Adaptive(self.store)

    def run_work(self):
        self.service.run_pending(*self.jobs[-1])

    def test_conversation_soft_focus_and_optional_checks_without_approval(self):
        def implement(worker, context, callbacks):
            self.assertIn('Sin aprobación formal', str(context['user_messages']))
            (worker.project / 'index.html').write_text('<h1>Agenda</h1><script src="app.js"></script>')
            (worker.project / 'app.js').write_text('document.title = "Agenda";')
            (worker.project / 'ARCHITECTURE.md').write_text('HTML estático; sin servidor. Revisado durante la implementación.')
            return checkpoint(focus='architecture', memory='Arquitectura revisada: página estática; no necesita servidor.')
        self.worker.responses = [checkpoint(focus='product', status='waiting_for_user',
            questions=[{'question': '¿Se usa sin conexión?', 'reason': 'Determina si necesitamos servicio externo.'}]),
            implement, checkpoint(focus='delivery', status='completed', next_step='',
                documents=['ARCHITECTURE.md'], tasks=[{'id': 'web', 'title': 'Página local', 'status': 'done'}])]
        self.service.submit_user_message('Hablemos de una agenda local.', self.pid, request_id='idea')
        self.run_work()
        self.assertEqual(self.service.get_status()['state'], 'waiting_for_user')
        question = self.service.list_pending_decisions()['items'][0]
        self.service.answer_decision(question['id'], 'Sí. Sin aprobación formal, impleméntala completa.')
        self.run_work()
        status = self.service.get_status()
        self.assertEqual(status['state'], 'completed')
        self.assertEqual(self.worker.calls, 3)
        self.assertEqual(len({r for p, r in self.jobs}), 1)
        self.assertEqual(status['checks'], [])
        self.assertFalse(status['completion']['independently_verified'])
        self.assertTrue((self.product / 'app.js').is_file())
        self.assertTrue(Path(status['completion']['delivery']).is_file())
        self.assertEqual(self.worker.contexts[-1]['checkpoint']['memory'], 'Arquitectura revisada: página estática; no necesita servidor.')
        self.assertEqual(self.worker.contexts[-1]['user_messages'], [])
        self.assertNotIn('Hablemos', json.dumps(self.worker.contexts[-1]))
        self.assertEqual(status['usage']['tokens'], 300)

    def test_failed_product_check_is_repaired_before_completion(self):
        def broken(worker, context, callbacks):
            (worker.project / 'main.py').write_text('print(missing_name)')
            result = subprocess.run([__import__('sys').executable, 'main.py'], cwd=worker.project, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            return checkpoint(status='completed', checks=[{'id': 'entry', 'description': 'Entrada real',
                'required': True, 'status': 'failed', 'evidence': 'NameError; proceso terminó con código 1.'}])
        def repair(worker, context, callbacks):
            self.assertEqual(context['checkpoint']['status'], 'continue')
            (worker.project / 'main.py').write_text('print("ready")')
            result = subprocess.run([__import__('sys').executable, 'main.py'], cwd=worker.project, capture_output=True, text=True)
            self.assertEqual(result.stdout.strip(), 'ready')
            return checkpoint(status='completed', checks=[{'id': 'entry', 'description': 'Entrada real',
                'required': True, 'status': 'passed', 'evidence': 'python main.py: ready; código 0.'}])
        self.worker.responses = [broken, repair]
        self.service.submit_user_message('Construye el producto y comprueba su entrada.')
        self.run_work()
        self.assertEqual(self.service.get_status()['state'], 'completed')
        self.assertEqual(self.worker.calls, 2)

    def test_message_during_work_is_not_lost_even_at_completion(self):
        def first(worker, context, callbacks):
            self.service.submit_user_message('Revisa la arquitectura: ahora lo necesito sin red.', request_id='steer')
            return checkpoint(status='completed', memory='Primera versión')
        def second(worker, context, callbacks):
            self.assertIn('sin red', str(context['user_messages']))
            return checkpoint(status='completed', memory='Arquitectura revisada por la nueva necesidad')
        self.worker.responses = [first, second]
        self.service.submit_user_message('Empieza', request_id='initial')
        self.run_work()
        self.assertEqual(self.worker.calls, 2)
        self.assertEqual(len(self.jobs), 1)

    def test_pause_idempotence_report_recovery_and_new_scope(self):
        self.service.pause()
        self.worker.responses = [checkpoint(status='completed')]
        self.service.submit_user_message('Una página', request_id='once')
        self.assertFalse(self.jobs)
        self.service.resume()
        self.run_work()
        original = self.service.get_status()
        report = Path(original['completion']['delivery'])
        content = report.read_bytes()
        report.unlink()
        for _ in range(2):
            self.service.submit_user_message('Una página', request_id='once')
            self.service.resume()
        self.assertEqual(self.worker.calls, 1)
        self.assertEqual(report.read_bytes(), content)
        self.assertEqual(self.service.get_status()['autonomous_run']['run_id'], original['autonomous_run']['run_id'])
        self.worker.responses = [checkpoint(status='completed', message='Segunda entrega')]
        self.service.submit_user_message('Añade un título', request_id='new-scope')
        self.run_work()
        self.assertEqual(self.worker.calls, 2)
        self.assertTrue(report.is_file())
        self.assertNotEqual(self.service.get_status()['completion']['delivery'], str(report))

    def test_saved_result_recovery_does_not_repeat_inference(self):
        self.worker.responses = [checkpoint(status='completed')]
        self.service.submit_user_message('Hazlo')
        with patch.object(Adaptive, 'apply', side_effect=OSError('crash after saving result')):
            with self.assertRaises(OSError):
                self.run_work()
        self.service.resume()
        self.run_work()
        self.assertEqual(self.worker.calls, 1)
        self.assertEqual(self.service.get_status()['state'], 'completed')

    def test_exact_runtime_turn_recovery_and_no_budget_reset(self):
        self.worker.responses = [FactoryError('transport_lost', 'Lost transport')]
        self.service.submit_user_message('Hazlo')
        with self.assertRaises(FactoryError):
            self.run_work()
        self.worker.recovered = {'response': checkpoint(status='completed'), 'usage': None}
        self.service.resume()
        self.run_work()
        self.assertEqual(self.worker.calls, 1)
        self.assertEqual(self.worker.recoveries[0]['thread_id'], 'thread-1')
        self.assertEqual(self.service.get_status()['usage']['tokens'], 100)

    def test_cumulative_optional_limit_survives_restart_and_can_be_extended(self):
        self.service.configure_project({'max_calls': 1})
        self.worker.responses = [checkpoint(), checkpoint(status='completed')]
        self.service.submit_user_message('Hazlo')
        with self.assertRaisesRegex(FactoryError, 'limit'):
            self.run_work()
        self.assertEqual(self.worker.calls, 1)
        self.service = FactoryService(self.service.registry, adaptive_worker_factory=self.worker,
            launcher=lambda p, r: self.jobs.append((p, r)))
        self.service.configure_project({'max_calls': 2})
        self.service.resume()
        self.run_work()
        self.assertEqual(self.worker.calls, 2)
        self.assertEqual(self.service.get_status()['usage']['tokens'], 200)

    def test_required_check_cannot_silently_disappear(self):
        check = {'id': 'chosen', 'description': 'Necesidad acordada', 'required': True,
                 'status': 'unavailable', 'evidence': 'Falta la cuenta externa'}
        self.worker.responses = [checkpoint(checks=[check]), checkpoint(status='completed'),
            checkpoint(status='waiting_for_user', checks=[check],
                       questions=[{'question': 'Crea la cuenta del servicio.', 'reason': 'No hay acceso real.'}])]
        self.service.submit_user_message('Implementa la integración')
        self.run_work()
        self.assertEqual(self.service.get_status()['state'], 'waiting_for_user')
        self.assertEqual(self.service.get_status()['checks'][0]['status'], 'unavailable')
        self.assertEqual(self.worker.contexts[-1]['checkpoint']['status'], 'continue')

    def test_input_added_to_prepared_step_is_seen_before_recovery_call(self):
        def recover(worker, context, callbacks):
            self.assertEqual([i['message'] for i in context['user_messages']], ['Prioriza la nueva condición'])
            return checkpoint(status='completed')
        self.service.configure_project({'max_calls': 1})
        self.worker.responses = [checkpoint(), recover]
        self.service.submit_user_message('Empieza')
        with self.assertRaises(FactoryError):
            self.run_work()
        self.service.pause()
        self.service.submit_user_message('Prioriza la nueva condición', request_id='during-budget-block')
        self.service.configure_project({'max_calls': 2})
        self.service.resume()
        self.run_work()
        self.assertEqual(self.service.get_status()['state'], 'completed')
        self.assertEqual(self.worker.calls, 2)
        with self.store._connection() as db:
            self.assertEqual(self.journal.pending(db), [])

    def test_registration_retry_preserves_old_projects_and_mcp_adaptive_default(self):
        old = self.root / 'historical'
        old.mkdir()
        Store(old).initialize()
        self.service.registry.register(str(old), trusted=True)
        self.service.initialize_project(str(old))
        self.assertFalse(enabled(Store(old)))
        tools = FactoryTools(self.service)
        new = tools.call('factory_project', {'action': 'init', 'path': str(self.root / 'second')})
        self.assertTrue(new['ok'])
        self.assertEqual(new['data']['workflow'], 'adaptive')
        self.assertTrue(tools.call('factory_project', {'action': 'configure', 'settings': {'effort': 'low'}})['ok'])
        self.assertTrue(tools.call('factory_inspect', {'view': 'process'})['ok'])

    def test_sdk_uses_normal_tools_and_inherits_configuration(self):
        from openai_codex import ApprovalMode, Sandbox
        with patch('openai_codex.Codex') as sdk:
            worker = CodexAdaptive(self.product)
        options = worker.thread_options(starting=True)
        self.assertEqual(options['sandbox'], Sandbox.full_access)
        self.assertEqual(options['approval_mode'], ApprovalMode.deny_all)
        self.assertNotIn('base_instructions', options)
        self.assertNotIn('model', options)
        self.assertNotIn('effort', worker.turn_options())
        config = sdk.call_args.kwargs['config']
        self.assertNotIn('features', str(config.config_overrides))
        self.assertNotIn('web_search', str(config.config_overrides))
        self.assertIn('software_factory', str(config.config_overrides))

    def test_cli_initializes_new_directory_and_reads_adaptive_context(self):
        path = self.root / 'from-cli'
        result = subprocess.run([sys.executable, '-m', 'factory', 'init', str(path)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['workflow'], 'adaptive')
        result = subprocess.run([sys.executable, '-m', 'factory', 'process', str(path)],
                                capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout)['focus'], 'idea')

    def test_pause_during_turn_preserves_partial_work_and_all_attempt_usage(self):
        def interrupted(worker, context, callbacks):
            (worker.project / 'partial.txt').write_text('work already done')
            self.service.pause()
            self.assertEqual(callbacks['should_stop'](), 'paused')
            raise FactoryError('paused', 'paused')
        def continued(worker, context, callbacks):
            self.assertIn('recovery', context)
            self.assertTrue((worker.project / 'partial.txt').exists())
            return checkpoint(status='completed')
        self.worker.responses = [interrupted, continued]
        self.service.submit_user_message('Hazlo')
        self.run_work()
        self.assertEqual(self.service.get_status()['state'], 'paused')
        self.service.resume()
        self.run_work()
        self.assertEqual(self.service.get_status()['usage']['tokens'], 200)
        self.assertEqual(self.service.get_status()['usage']['calls'], 2)

    def test_checkpoint_format_is_repaired_without_user_approval(self):
        def repair(worker, context, callbacks):
            self.assertIn('checkpoint_repair', context)
            return checkpoint(status='completed')
        self.worker.responses = [checkpoint(memory='x' * 12001), repair]
        self.service.submit_user_message('Hazlo')
        self.run_work()
        self.assertEqual(self.service.get_status()['state'], 'completed')
        self.assertEqual(self.service.get_status()['usage']['tokens'], 200)

    def test_explained_check_revision_is_allowed_and_keeps_history(self):
        original = {'id': 'remote', 'description': 'Servicio remoto', 'required': True, 'status': 'pending', 'evidence': ''}
        self.worker.responses = [checkpoint(checks=[original]), checkpoint(status='completed',
            revisions=[{'check_id': 'remote', 'reason': 'La solución acordada es local y ya no utiliza un servicio remoto.'}])]
        self.service.submit_user_message('Simplifica el producto si se puede resolver localmente')
        self.run_work()
        self.assertEqual(self.service.get_status()['state'], 'completed')
        with self.store._connection() as db:
            steps = self.journal.steps(db)
        self.assertEqual(steps[0]['result']['response']['checks'], [original])


class AdaptiveMCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_detached_adaptive_steps_continue_after_real_mcp_disconnect(self):
        from mcp import Client
        from mcp.client.stdio import StdioServerParameters
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            repo = Path(__file__).resolve().parent.parent
            registry = Registry(root / 'registry')
            started, release = root / 'started', root / 'release'
            worker = root / 'worker-python'
            worker.write_text(f'''#!{sys.executable}
import sys,time
from pathlib import Path
sys.path.insert(0, {str(repo)!r})
from factory.application import FactoryService
from tests.test_adaptive import FakeCodex, checkpoint
def first(worker, context, callbacks):
 Path({str(started)!r}).touch()
 deadline=time.monotonic()+15
 while not Path({str(release)!r}).exists() and time.monotonic()<deadline:
  time.sleep(.05)
 (worker.project/'index.html').write_text('<h1>Detached product</h1>')
 return checkpoint(focus='design')
fake=FakeCodex([first,checkpoint(status='completed',focus='delivery')])
original=FactoryService.__init__
def init(self,*a,**kw):
 kw['adaptive_worker_factory']=fake
 original(self,*a,**kw)
FactoryService.__init__=init
sys.argv=['factory.runner',*sys.argv[3:]]
from factory.runner import main
main()
''')
            worker.chmod(0o700)
            bootstrap = root / 'server.py'
            bootstrap.write_text(f'''import sys
sys.path.insert(0,{str(repo)!r})
import factory.application
factory.application.sys.executable={str(worker)!r}
from factory.mcp_server import main
main()
''')
            params = StdioServerParameters(command=sys.executable, args=[str(bootstrap), '--home', str(registry.home)])
            async with Client(params) as client:
                result = await client.call_tool('factory_project', {'action': 'init', 'path': str(root / 'product')})
                self.assertTrue(result.structured_content['ok'])
                await client.call_tool('factory_message', {'message': 'Crea una página', 'request_id': 'once'})
                for _ in range(100):
                    if started.exists():
                        break
                    await asyncio.sleep(.05)
                self.assertTrue(started.exists())
            release.touch()  # The MCP process and connection are already closed.
            service = FactoryService(registry)
            for _ in range(100):
                if service.get_status()['state'] == 'completed':
                    break
                await asyncio.sleep(.05)
            self.assertEqual(service.get_status()['state'], 'completed')
            self.assertEqual(service.get_status()['usage']['calls'], 2)
            self.assertTrue((root / 'product/index.html').is_file())
            async with Client(params) as client:
                result = await client.call_tool('factory_resume', {})
                self.assertEqual(result.structured_content['data']['usage']['calls'], 2)


if __name__ == '__main__':
    unittest.main()
