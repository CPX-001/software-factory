from copy import deepcopy
import json
from pathlib import Path
import tempfile
import time
import unittest
import threading
import subprocess
import fcntl
from unittest.mock import patch

from factory.execution import Execution, current_sources, select_slice
from factory.execution_contract import validate_result, validate_verification
from factory.execution_sandbox import LinuxSandbox
from factory.execution_store import ExecutionStore
from factory.execution_workspace import code_identity
from factory.registry import FactoryError
from factory.runtime import Runtime
from factory.codex_execution import quota_guard
from tests.execution_fakes import fixture, FakeSDK, result


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def setup_product(self, *responses, **kwargs):
        self.sdk = FakeSDK(*(responses or (result(),)))
        self.service, self.store, self.jobs, self.git = fixture(self.root, self.sdk, **kwargs)
        self.journal = ExecutionStore(self.store)

    def run_product(self):
        self.service.execute_next_slice(request_id='first')
        self.service.run_pending(*self.jobs[-1])
        return self.journal.latest()

    def test_quota_transport_cause_survives_restart_and_status_without_inference(self):
        from openai_codex.errors import InternalRpcError
        from factory.application import FactoryService
        from factory.registry import Registry
        self.setup_product()
        with patch.object(self.sdk, 'quota', side_effect=InternalRpcError(-32603,
                'failed to fetch codex rate limits: error sending request secret=SYNTHETIC_SECRET')):
            data = self.run_product()
        self.assertEqual(data['state'], 'quota_blocked')
        self.assertEqual(data['attempt'], 0)
        reader = FactoryService(Registry(self.service.registry.home))
        status = reader.get_status()
        self.assertEqual(status['execution']['diagnostic']['code'], 'quota_transport')
        self.assertEqual(status['execution']['diagnostic']['rpc_code'], -32603)
        self.assertIn('network/TLS', status['next_action']['next_step'])
        self.assertNotIn('SYNTHETIC_SECRET', json.dumps(status))
        self.assertEqual(self.sdk.contexts, [])

    def test_localized_context_request_reuses_slice_session_and_explains_denied_reads(self):
        request = result(); request.update(changes=[], read_paths=['detail.txt', 'unapproved.txt'])
        self.setup_product(request, result())
        detail = 'Localized existing product rule\n' * 250
        (self.store.project / 'detail.txt').write_text(detail)
        for name in ('a1.txt', 'a2.txt', 'a3.txt'):
            (self.store.project / name).write_text('Existing notes\n' * 780)
        self.git('add', 'detail.txt', 'a1.txt', 'a2.txt', 'a3.txt'); self.git('commit', '-qm', 'Product context')
        policy = {k: v for k, v in self.journal.policy().items() if k not in ('repository', 'definition_id', 'authorized_at')}
        policy['context_paths'].extend(['detail.txt', 'a1.txt', 'a2.txt', 'a3.txt'])
        definition = self.journal.definition(self.journal.policy()['definition_id'])['verification']
        self.service.configure_execution(policy, definition)
        data = self.run_product()
        self.assertEqual(data['state'], 'checkpoint', data)
        repair = self.sdk.contexts[1]
        self.assertNotIn('detail.txt', self.sdk.contexts[0]['files'])
        self.assertEqual(repair['files']['detail.txt'], detail)
        self.assertEqual(repair['context_requests']['unavailable']['unapproved.txt'], 'outside_authorized_context')
        self.assertNotIn('architecture', repair)
        self.assertNotIn('requirements', repair)
        self.assertEqual(self.sdk.threads, [None, 'fake-slice-thread'])

    def test_application_error_is_returned_to_worker_within_repair_budget(self):
        bad = result(); bad['changes'][0]['path'] = 'app.py/nested.py'
        self.setup_product(bad, result())
        data = self.run_product()
        self.assertEqual(data['state'], 'checkpoint', data)
        self.assertEqual(data['attempt'], 2)
        self.assertEqual(self.sdk.contexts[1]['failure']['kind'], 'application_conflict')
        self.assertEqual(self.sdk.contexts[1]['failure']['path'], 'app.py/nested.py')
        self.assertIn('def add', self.sdk.contexts[0]['files']['app.py'])

    def test_unsupported_capabilities_rejected_before_runtime_or_quota(self):
        from factory.verification import capability_errors
        self.setup_product()
        snapshot = self.store.snapshot()
        plan = snapshot['planning']['roadmap']['plan']
        baseline = snapshot['architecture']['baseline']['architecture']
        policy = self.journal.policy()
        definition = self.journal.definition(policy['definition_id'])['verification']
        for capability in ('Node.js', 'Browser Playwright', 'Docker', 'pytest'):
            changed = deepcopy(plan); changed['harness'][0]['capability'] = capability
            self.assertTrue(capability_errors(changed, changed['slices'][0], definition, policy, baseline))
        definition['checks'][0]['kind'] = 'specialist'
        self.assertIn('specialist_review_unsupported:behavior', capability_errors(plan, plan['slices'][0], definition, policy, baseline))
        self.service.configure_execution({k: v for k, v in policy.items() if k not in ('repository', 'definition_id', 'authorized_at')}, definition)
        with self.assertRaises(FactoryError) as exc:
            self.service.execute_next_slice(request_id='unsupported')
        self.assertEqual(exc.exception.code, 'capability_unavailable')
        self.assertEqual(self.jobs, [])
        self.assertEqual(self.sdk.contexts, [])
        self.assertEqual(self.service.get_status()['execution']['diagnostic']['code'], 'capability_unavailable')

    def test_real_failure_repair_and_only_then_acceptance(self):
        self.setup_product(result(0), result(3))
        user_head = self.git('rev-parse', 'HEAD')
        (self.store.project / 'user-notes.txt').write_text('keep my work')
        data = self.run_product()
        self.assertEqual(data['state'], 'checkpoint', data)
        self.assertEqual(data['attempt'], 2)
        attempts = self.journal.attempts(data['id'])
        self.assertEqual(attempts[0]['worker_status'], 'completed')
        self.assertEqual(attempts[0]['verification_status'], 'FAIL')
        self.assertEqual(attempts[1]['verification_status'], 'PASS')
        self.assertIn('Behavior mismatch', self.sdk.contexts[1]['failure']['checks'][0]['log'])
        self.assertEqual(self.sdk.threads, [None, 'fake-slice-thread'])
        self.assertEqual(self.git('rev-parse', 'HEAD'), user_head)
        self.assertEqual((self.store.project / 'user-notes.txt').read_text(), 'keep my work')
        self.assertIn('return 3', self.git('show', data['commit'] + ':app.py'))
        self.assertEqual(len(self.journal.acceptances()), 1)
        self.assertEqual(self.store.snapshot()['phase'], 'execution')
        self.assertEqual(self.store.snapshot()['planning']['roadmap']['plan']['milestones'][0]['status'], 'open')

    def test_harness_is_built_by_its_own_slice(self):
        self.setup_product(result(0, tests=True), result(3), harness_during=True)
        data = self.run_product()
        self.assertEqual(data['state'], 'checkpoint', data)
        self.assertEqual(data['attempt'], 2)
        self.assertIn('test_app.py', data['protected_files'])

    def test_frozen_unittest_oracle_supports_clean_slice_harness_and_detects_tampering(self):
        import hashlib
        from factory.verification import check_coverage
        self.setup_product(harness_during=True)
        test = self.store.project / 'test_app.py'
        test.write_text('import unittest\nfrom app import add\nclass Test(unittest.TestCase):\n def test_sum(self): self.assertEqual(add(1, 2), 3)\n')
        self.git('add', 'test_app.py'); self.git('commit', '-qm', 'Independent predeclared oracle')
        policy = {k: v for k, v in self.journal.policy().items() if k not in ('repository', 'definition_id', 'authorized_at')}
        policy['write_paths'] = ['app.py']
        definition = self.journal.definition(self.journal.policy()['definition_id'])['verification']
        check = deepcopy(next(c for c in definition['checks'] if c['kind'] == 'python_unittest'))
        check.update(source_sha256=hashlib.sha256(test.read_bytes()).hexdigest(), clean_copy=True, criteria=[0])
        definition['checks'] = [check]
        plan = self.store.snapshot()['planning']['roadmap']['plan']
        self.assertNotIn('independent_acceptance_oracle_missing', check_coverage(plan, plan['slices'][0], definition))
        self.service.configure_execution(policy, definition)
        data = self.run_product()
        self.assertEqual(data['state'], 'checkpoint', data)
        evidence = data['verification'][0]
        self.assertEqual(evidence['reproducibility']['code_id'], evidence['code_id'])
        self.assertEqual(self.git('rev-parse', data['commit'] + '^{tree}'), evidence['reproducibility']['commit'])
        test.write_text(test.read_text().replace('add(1, 2), 3', 'True, True'))
        with self.assertRaises(FactoryError) as exc:
            self.service.configure_execution(policy, definition)
        self.assertEqual(exc.exception.code, 'verification_weakened')

    def test_identical_procedure_reuses_only_same_code_and_profile(self):
        from factory.verification import Verifier
        self.setup_product()
        policy = self.journal.policy()
        definition = self.journal.definition(policy['definition_id'])['verification']
        check = deepcopy(next(c for c in definition['checks'] if c['kind'] == 'python_behavior'))
        definition['checks'] = [check, {**check, 'id': 'same_procedure'}]
        self.service.configure_execution({k:v for k,v in policy.items() if k not in ('repository','definition_id','authorized_at')}, definition)
        data = self.run_product()
        self.assertEqual(data['state'], 'checkpoint', data)
        self.assertEqual(data['verification'][1]['reused_from'], check['id'])

    def test_duplicate_mcp_intent_and_finished_request_are_idempotent(self):
        self.setup_product()
        from factory.mcp_server import FactoryTools
        tool = FactoryTools(self.service)
        a = tool.call('factory_execute', {'request_id': 'same'})
        b = tool.call('factory_execute', {'request_id': 'same'})
        c = tool.call('factory_execute', {'request_id': 'different-client'})
        self.assertTrue(a['ok'], a)
        self.assertEqual(len(self.jobs), 1)
        self.assertEqual(a['data']['execution']['id'], b['data']['execution']['id'])
        self.assertEqual(a['data']['execution']['id'], c['data']['execution']['id'])
        self.service.run_pending(*self.jobs[0])
        tool.call('factory_execute', {'request_id': 'same'})
        self.assertEqual(len(self.jobs), 1)
        self.service.run_pending(*self.jobs[0])
        self.assertEqual(len(self.sdk.contexts), 1)

    def test_same_failure_without_changes_stops_early(self):
        self.setup_product(result(0), result(0), result(3))
        data = self.run_product()
        self.assertEqual(data['state'], 'failed', data)
        self.assertEqual(data['attempt'], 2)
        self.assertFalse(self.journal.acceptances())

    def test_three_attempts_exhaustion_survives_resume(self):
        self.setup_product(result(0), result(1), result(2), result(3))
        data = self.run_product()
        self.assertEqual(data['state'], 'budget_exhausted', data)
        self.service.resume(); self.service.run_pending(*self.jobs[-1])
        self.assertEqual(len(self.sdk.contexts), 3)
        self.assertEqual(self.journal.latest()['state'], 'budget_exhausted')

    def test_invalid_outputs_count_toward_the_same_budget(self):
        invalid = result(); invalid['criteria_addressed'] = ['invalid']
        third = result(); third['changes'][0]['operation'] = 'unknown-operation'
        self.setup_product({}, invalid, third)
        data = self.run_product()
        self.assertEqual(data['state'], 'budget_exhausted', data)
        self.assertEqual(len(self.journal.attempts(data['id'])), 3)

    def test_repeated_invalid_output_stops_before_third_attempt(self):
        self.setup_product({}, {}, result())
        data = self.run_product()
        self.assertEqual(data['state'], 'failed', data)
        self.assertEqual(data['attempt'], 2)

    def test_cosmetic_changes_do_not_reset_repeated_failure_detection(self):
        cosmetic = result(0); cosmetic['changes'][0]['content'] += '# claimed repair\n'
        self.setup_product(result(0), cosmetic, result())
        data = self.run_product()
        self.assertEqual(data['state'], 'failed', data)
        self.assertEqual(data['attempt'], 2)

    def test_cannot_weaken_existing_tests(self):
        bad = result()
        bad['changes'].append({'path': 'test_app.py', 'operation': 'write', 'content': 'assert True\n'})
        self.setup_product(bad)
        data = self.run_product()
        self.assertEqual(data['state'], 'blocked', data)
        self.assertIn('verification_weakened', data['blockers'])
        self.assertFalse(self.journal.acceptances())

    def test_worker_cannot_write_controller_or_escape_scope(self):
        for path in ('../state.sqlite3', '.factory/state.sqlite3', '/tmp/escaped', '.codex/config.toml'):
            bad = result(); bad['changes'][0]['path'] = path
            with self.assertRaises(FactoryError):
                validate_result(bad)
        self.setup_product(result())
        self.sdk.responses[0]['changes'][0]['path'] = 'not-authorized.py'
        data = self.run_product()
        self.assertIn('scope_violation', data['blockers'])

    def test_unknown_quota_and_wrong_auth_are_blockers_without_model_calls(self):
        self.setup_product()
        self.sdk.quota_value = {'observed_at': time.time(), 'buckets': {}}
        data = self.run_product()
        self.assertEqual(data['state'], 'quota_blocked', data)
        self.assertFalse(self.sdk.contexts)
        self.sdk.quota_value = None
        self.sdk.init_error = FactoryError('authentication', 'wrong auth')
        self.service.resume(); self.service.run_pending(*self.jobs[-1])
        self.assertEqual(self.journal.latest()['state'], 'blocked')
        self.assertFalse(self.sdk.contexts)

    def test_runtime_failure_is_not_automatically_treated_as_a_code_repair(self):
        self.setup_product(FactoryError('infrastructure_failed', 'transport disconnected'), result())
        data = self.run_product()
        self.assertEqual(data['state'], 'infrastructure_failed', data)
        self.assertEqual(data['attempt'], 1)
        self.assertEqual(len(self.sdk.contexts), 1)

    def test_quota_stale_reserve_expired_and_missing_windows(self):
        self.setup_product()
        policy = self.journal.policy()
        for quota in ({}, {'observed_at': time.time()-1000},
            {'observed_at': time.time(), 'buckets': {'codex': {}}},
            {'observed_at': time.time(), 'buckets': {'codex': {'primary': {'usedPercent': 90, 'resetsAt': time.time()+500}}}},
            {'observed_at': time.time(), 'buckets': {'codex': {'primary': {'usedPercent': 5, 'resetsAt': 1}}}}):
            with self.assertRaises(FactoryError):
                quota_guard(quota, policy)

    def test_publication_failure_recovers_same_commit_without_another_worker(self):
        self.setup_product()
        import factory.execution as module
        real = module.publish_ref
        def publish_then_fail(*args):
            real(*args)
            raise OSError('crash between Git and SQLite')
        with patch('factory.execution.publish_ref', side_effect=publish_then_fail):
            data = self.run_product()
        commit = data['publication']['commit']
        self.assertEqual(data['state'], 'infrastructure_failed')
        self.assertFalse(self.journal.acceptances())
        self.service.resume(); self.service.run_pending(*self.jobs[-1])
        self.assertEqual(self.journal.latest()['state'], 'checkpoint', self.journal.latest())
        self.assertEqual(self.journal.latest()['commit'], commit)
        self.assertEqual(len(self.sdk.contexts), 1)

    def test_changed_code_invalidates_saved_pass(self):
        self.setup_product()
        with patch.object(Execution, 'accept', side_effect=OSError('crash before commit')):
            data = self.run_product()
        (Path(data['worktree']) / 'app.py').write_text('def add(a,b): return 0\n')
        with self.assertRaisesRegex(FactoryError, 'exact code'):
            Execution(self.service, self.store).accept(data)
        self.assertFalse(self.journal.acceptances())

    def test_new_dependency_is_retained_as_architectural_proposal(self):
        self.setup_product()
        policy = {k: v for k, v in self.journal.policy().items() if k not in ('repository', 'definition_id', 'authorized_at')}
        policy['write_paths'].append('requirements.txt')
        definition = self.journal.definition(self.journal.policy()['definition_id'])['verification']
        self.service.configure_execution(policy, definition)
        self.sdk.responses[0]['changes'].append({'path': 'requirements.txt', 'operation': 'write', 'content': 'new-database\n'})
        data = self.run_product()
        self.assertEqual(data['state'], 'blocked', data)
        self.assertEqual(data['architecture_proposal']['paths'], ['requirements.txt'])
        self.assertTrue((Path(data['worktree']) / 'requirements.txt').exists())
        self.assertFalse(self.journal.acceptances())

    def test_outline_dependencies_and_stale_sources_are_not_reinterpreted(self):
        self.setup_product()
        snapshot = self.store.snapshot()
        plan = snapshot['planning']['roadmap']['plan']
        plan['slices'][0]['dependencies'] = ['unaccepted']
        snapshot['planning']['roadmap']['fingerprint'] = __import__('factory.architecture', fromlist=['fingerprint']).fingerprint(plan)
        selected, reasons = select_slice(snapshot, [])
        self.assertIsNone(selected)
        self.assertEqual(reasons[0]['reason'], 'dependencies_unaccepted')
        plan['slices'][0]['maturity'] = 'outline'
        snapshot['planning']['roadmap']['fingerprint'] = __import__('factory.architecture', fromlist=['fingerprint']).fingerprint(plan)
        self.assertEqual(select_slice(snapshot, [])[1][0]['reason'], 'refinement_required')
        plan['architecture']['revision'] += 1
        with self.assertRaises(FactoryError):
            current_sources(snapshot)

    def test_specialist_required_gate_blocks_before_spending_quota(self):
        self.setup_product()
        policy = {k: v for k, v in self.journal.policy().items() if k not in ('repository', 'definition_id', 'authorized_at')}
        definition = self.journal.definition(self.journal.policy()['definition_id'])['verification']
        definition['checks'][0].update(kind='specialist', target='security-review', cases=[])
        self.service.configure_execution(policy, definition)
        with self.assertRaises(FactoryError) as exc:
            self.service.execute_next_slice(request_id='specialist')
        self.assertEqual(exc.exception.code, 'capability_unavailable')
        self.assertEqual(self.sdk.contexts, [])
        self.assertFalse(self.journal.acceptances())

    def test_pause_interrupts_attempt_and_resume_inspects_saved_work(self):
        self.setup_product(result(), result())
        def pause(stop):
            self.service.pause()
            self.assertEqual(stop(), 'paused')
            raise FactoryError('paused', 'turn interrupted')
        self.sdk.on_call = pause
        data = self.run_product()
        self.assertEqual(data['state'], 'paused', data)
        self.assertEqual(len(self.sdk.contexts), 1)
        self.sdk.on_call = None
        self.service.resume(); self.service.run_pending(*self.jobs[-1])
        self.assertEqual(self.journal.latest()['state'], 'checkpoint', self.journal.latest())
        self.assertEqual(self.journal.latest()['attempt'], 2)

    def test_stale_worker_cannot_publish_result_after_recovery(self):
        self.setup_product()
        self.service.execute_next_slice(request_id='old')
        data = self.journal.latest()
        attempt = self.journal.start_attempt(data)
        Runtime(self.store).update(data['run_id'], 'interrupted')
        self.service.resume()
        with self.assertRaises(FactoryError):
            self.journal.save_attempt(data, attempt)

    def test_upgrade_does_not_authorize_execution(self):
        from factory.workflow import Store
        store = Store(self.root); store.initialize()
        self.assertFalse(ExecutionStore(store).policy()['enabled'])
        store.initialize()
        self.assertFalse(ExecutionStore(store).policy()['enabled'])

    def test_no_eligible_slice_does_not_invoke_a_model(self):
        self.setup_product()
        self.run_product()
        with self.assertRaises(FactoryError) as error:
            self.service.execute_next_slice(request_id='next')
        self.assertEqual(error.exception.code, 'no_eligible_slice')
        self.assertEqual(len(self.sdk.contexts), 1)

    def test_status_and_duplicate_request_do_not_start_concurrent_worker(self):
        self.setup_product()
        entered, released = threading.Event(), threading.Event()
        def wait(stop):
            entered.set()
            self.assertTrue(released.wait(5))
        self.sdk.on_call = wait
        self.service.execute_next_slice(request_id='a')
        running = threading.Thread(target=lambda: self.service.run_pending(*self.jobs[0]))
        running.start()
        self.assertTrue(entered.wait(5))
        try:
            self.service.get_status()
            self.service.execute_next_slice(request_id='b')
            self.assertEqual(len(self.jobs), 1)
        finally:
            released.set(); running.join(10)
        self.assertFalse(running.is_alive())
        self.assertEqual(self.journal.latest()['state'], 'checkpoint')

    def test_orphan_lease_blocks_relaunch_even_when_controller_is_gone(self):
        self.setup_product()
        self.service.execute_next_slice(request_id='orphan')
        data = self.journal.latest()
        Runtime(self.store).update(data['run_id'], 'interrupted')
        sandbox = LinuxSandbox(self.store.path.parent / 'executions' / data['id'])
        with (sandbox.directory / 'process.lock').open('a') as lease:
            fcntl.flock(lease, fcntl.LOCK_EX)
            self.service.resume()
            self.assertEqual(len(self.jobs), 1)
        self.service.resume()
        self.assertEqual(len(self.jobs), 2)

    def test_saved_response_replays_without_repeating_model_after_apply_crash(self):
        self.setup_product()
        with patch('factory.execution.apply_changes', side_effect=OSError('interrupted application')):
            data = self.run_product()
        self.assertEqual(data['state'], 'infrastructure_failed')
        self.assertEqual(self.journal.attempts(data['id'])[0]['state'], 'responded')
        self.service.resume(); self.service.run_pending(*self.jobs[-1])
        self.assertEqual(self.journal.latest()['state'], 'checkpoint')
        self.assertEqual(len(self.sdk.contexts), 1)

    def test_runtime_completed_turn_is_recovered_before_spending_another_attempt(self):
        self.setup_product()
        self.service.execute_next_slice(request_id='recover-runtime')
        data = self.journal.latest()
        attempt = self.journal.start_attempt(data)
        attempt['runtime'] = {'thread_id': 'saved-thread', 'turn_id': 'saved-turn', 'process': None}
        self.journal.save_attempt(data, attempt)
        Runtime(self.store).update(data['run_id'], 'interrupted')
        self.sdk.recovered_result = {'response': result(), 'worker_status': 'completed', 'usage': None}
        self.service.resume(); self.service.run_pending(*self.jobs[-1])
        self.assertEqual(self.journal.latest()['state'], 'checkpoint', self.journal.latest())
        self.assertEqual(self.journal.latest()['attempt'], 1)
        self.assertFalse(self.sdk.contexts)

    def test_human_answer_continues_authorized_slice_without_another_resume(self):
        question = result(); question['questions'] = ['Keep integer output?']
        self.setup_product(question, result())
        data = self.run_product()
        self.assertEqual(data['state'], 'waiting_decision', data)
        decision = self.service.list_pending_decisions()['items'][0]
        self.service.answer_decision(decision['id'], 'Yes')
        self.assertEqual(len(self.jobs), 2)
        self.service.run_pending(*self.jobs[-1])
        self.assertEqual(self.journal.latest()['state'], 'checkpoint', self.journal.latest())
        self.assertEqual(self.sdk.contexts[-1]['answers'][0]['answer'], 'Yes')

    def test_missing_required_skill_blocks_before_a_model_call(self):
        self.setup_product()
        from factory.skill_router import Policy, SkillRouter
        from factory.skill_catalog import Catalog
        self.service.router = SkillRouter(Catalog(), Policy(required=('absent-skill',)))
        data = self.run_product()
        self.assertNotEqual(data['state'], 'checkpoint')
        self.assertFalse(self.sdk.contexts)

    def test_missing_preexisting_harness_blocks_without_worker(self):
        self.setup_product()
        # Change the authorized baseline before starting, preserving the repository identity.
        (self.store.project / 'test_app.py').unlink()
        self.git('add', 'test_app.py'); self.git('commit', '-qm', 'Remove unavailable harness')
        data = self.run_product()
        self.assertEqual(data['state'], 'blocked')
        self.assertIn('harness_unavailable:unit', data['blockers'])
        self.assertFalse(self.sdk.contexts)

    def test_duration_and_observed_usage_are_persistent_limits(self):
        self.setup_product()
        self.sdk.usage = {'total': {'totalTokens': self.journal.policy()['max_tokens']}}
        data = self.run_product()
        self.assertEqual(data['state'], 'budget_exhausted', data)
        self.assertFalse(self.journal.acceptances())
        self.service.resume(); self.service.run_pending(*self.jobs[-1])
        self.assertEqual(len(self.sdk.contexts), 1)

    def test_expired_deadline_survives_restart_without_any_model_call(self):
        self.setup_product()
        self.service.execute_next_slice(request_id='expired')
        data = self.journal.latest()
        data['deadline'] = time.time() - 1
        self.journal.save(data)
        Runtime(self.store).update(data['run_id'], 'interrupted')
        self.service.resume(); self.service.run_pending(*self.jobs[-1])
        self.assertEqual(self.journal.latest()['state'], 'budget_exhausted')
        self.assertFalse(self.sdk.contexts)

    def test_pinned_verification_cannot_accept_arbitrary_shell_or_omit_coverage(self):
        self.setup_product()
        definition = self.journal.definition(self.journal.policy()['definition_id'])['verification']
        definition['checks'][0]['kind'] = 'shell'
        with self.assertRaises(Exception):
            validate_verification(definition, self.store.snapshot()['planning']['roadmap']['plan'])

    def test_behavior_target_cannot_be_used_as_a_system_shell_tool(self):
        self.setup_product()
        from factory.verification import Verifier
        definition = self.journal.definition(self.journal.policy()['definition_id'])['verification']
        definition['checks'] = [definition['checks'][0]]
        definition['checks'][0].update(target='os:system', cases=[{'args_json': '["true"]', 'expected_json': '0'}])
        snapshot = self.store.snapshot()
        product = self.root / 'callable-product'; product.mkdir()
        evidence = Verifier(LinuxSandbox(self.root / 'checks')).run(snapshot['planning']['roadmap']['plan'],
            snapshot['planning']['roadmap']['plan']['slices'][0], definition, product,
            trigger='after_slice', should_stop=lambda: False, on_process=lambda ref: None, remaining=lambda: 10)
        self.assertEqual(evidence[0]['status'], 'NOT_RUN')

    def test_zero_tests_and_early_exit_zero_do_not_mean_pass(self):
        self.setup_product()
        from factory.verification import Verifier
        definition = self.journal.definition(self.journal.policy()['definition_id'])['verification']
        product = self.root / 'isolated-product'; product.mkdir()
        (product / 'test_app.py').write_text('import unittest\n')
        (product / 'app.py').write_text('import os\nos._exit(0)\n')
        snapshot = self.store.snapshot()
        evidence = Verifier(LinuxSandbox(self.root / 'checks')).run(snapshot['planning']['roadmap']['plan'],
            snapshot['planning']['roadmap']['plan']['slices'][0], definition, product,
            trigger='after_slice', should_stop=lambda: False, on_process=lambda ref: None, remaining=lambda: 10)
        self.assertEqual([e['status'] for e in evidence], ['NOT_RUN', 'NOT_RUN'], evidence)

    def test_one_slice_checkpoint_even_with_two_prepared_slices(self):
        def customize(plan):
            future = deepcopy(plan['slices'][0]); future.update(id='s2', dependencies=['s1'])
            plan['slices'].append(future); plan['near_term'].append('s2')
            gate = deepcopy(plan['gates'][0]); gate.update(id='local_second', target='s2', harness=[])
            plan['gates'].append(gate)
            for coverage in plan['coverage']:
                coverage['slices'].append('s2')
        self.setup_product(result(), customize=customize)
        data = self.run_product()
        self.assertEqual(data['state'], 'checkpoint', data)
        self.assertEqual(self.service.get_planning(view='next_slice')['slice']['id'], 's2')
        self.assertEqual(len(self.sdk.contexts), 1)


class IsolationTests(unittest.TestCase):
    def test_runtime_can_start_threads_but_cannot_spawn_child_programs(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = LinuxSandbox(tmp)
            code = '''import subprocess, threading
t=threading.Thread(target=lambda:None); t.start(); t.join()
try:
    subprocess.run(['/bin/true'], check=True)
except PermissionError:
    print('process-spawn-denied')
else:
    raise AssertionError('runtime spawned a child process')
'''
            command = sandbox.command(['/usr/bin/python3', '-I', '-c', code], mode='runtime')
            value = subprocess.run(command, capture_output=True, text=True, timeout=10)
            self.assertEqual(value.returncode, 0, value.stderr)
            self.assertIn('process-spawn-denied', value.stdout)

    def test_real_sandbox_hides_state_denies_writes_and_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / 'controller.sqlite3'; state.write_text('authoritative')
            product = root / 'product'; product.mkdir()
            (product / 'code.py').write_text('original')
            code = f'''import os, socket
assert not os.path.exists({str(state)!r})
assert not os.path.exists('/proc/1/root{str(state)}')
try:
    open('/workspace/code.py','w').write('forged')
except OSError:
    pass
else:
    raise AssertionError('writable code')
try:
    socket.create_connection(('1.1.1.1',443), timeout=.1)
except OSError:
    pass
else:
    raise AssertionError('network escaped')
print('protected')
'''
            sandbox = LinuxSandbox(root / 'sandbox')
            outcome = sandbox.run(['/usr/bin/python3', '-I', '-c', code], [(str(product), '/workspace', False)])
            self.assertEqual(outcome['status'], 'PASS', outcome)
            self.assertEqual(state.read_text(), 'authoritative')
            self.assertEqual((product / 'code.py').read_text(), 'original')

    def test_timeout_is_not_pass_and_leaves_no_live_lease(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = LinuxSandbox(tmp)
            value = sandbox.run(['/usr/bin/python3', '-I', '-c', 'import time; time.sleep(30)'], timeout=.2)
            self.assertEqual(value['status'], 'NOT_RUN')
            self.assertEqual(value['reason'], 'timeout')
            self.assertFalse(sandbox.live())
