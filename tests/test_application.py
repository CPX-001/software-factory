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
    def blocked_recovery(self, *, schema_rejection=False):
        from factory.architecture import fingerprint
        from tests.planning_fakes import dynamic
        def bad_plan(context):
            plan = dynamic(context)
            plan['risks'] = [{'id': 'contract_drift', 'severity': 'medium', 'description': 'Shared contract drift',
                'owner': 'app', 'mitigation': 'Use the shared implementation', 'acceptance_key': '',
                'validation_slice': 's1', 'blocks': []}]
            plan['slices'][0]['risks'] = ['contract_drift']
            return plan
        self.sdk.responses = [complete_reply(), lambda c: proposal(c['source']['knowledge']), review(),
                              bad_plan, review(), bad_plan, review(), bad_plan, review()]
        if schema_rejection:
            error = {'codex_error_info':'other','message':json.dumps({'status':400,'error':{
                'type':'invalid_request_error','code':'invalid_json_schema','param':'text.format.schema'}})}
            self.sdk.responses.insert(3, FactoryError('infrastructure_failed', str(error)))
        self.start()
        if schema_rejection:
            with self.assertRaises(FactoryError): self.service.run_pending(*self.jobs[-1])
            self.service.resume(self.identifier)
        self.service.run_pending(*self.jobs[-1])
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot['planning']['stage'], 'blocked')
        group = self.service.get_status(self.identifier)['continuation']
        request = {'request_id': 'reviewed-recovery-once', 'run_id': group['runtime_id'],
            'proposal_fingerprint': fingerprint(snapshot['planning']['proposal']),
            'reason': 'Operator authorizes one correction/review of the saved invalid owner; retain original checks.'}
        policy = deepcopy(self.policy); policy['continuation']['max_seconds'] += 1800
        return snapshot, group, policy, request

    @staticmethod
    def repaired_owner(context):
        plan = deepcopy(context['proposal']); plan['risks'][0]['owner'] = 's1'
        return plan

    def test_operator_recovery_continues_same_run_and_preserves_history(self):
        snapshot, before, policy, request = self.blocked_recovery(schema_rejection=True)
        self.assertEqual(snapshot['planning']['calls'], 7)
        jobs = len(self.jobs)
        with self.store._connection() as db:
            history = [tuple(r) for r in db.execute('SELECT * FROM planning_calls ORDER BY id')]
        self.sdk.responses = [self.repaired_owner, review()]
        with patch('time.time', return_value=before['deadline'] + 1):
            self.service.configure_execution(policy, self.verification, self.identifier, planning_recovery=request)
            self.assertEqual(self.jobs[-1][1], before['runtime_id'])
            # A retry while queued grants no extra calls and creates no additional job.
            self.service.configure_execution(policy, self.verification, self.identifier, planning_recovery=request)
            self.assertEqual(len(self.jobs), jobs + 1)
            self.service.run_pending(*self.jobs[-1])
        after = self.service.get_status(self.identifier)['continuation']
        self.assertEqual(after['id'], before['id'])
        self.assertEqual(after['deadline'], before['deadline'] + 1800)
        self.assertEqual(after['created_at'], before['created_at'])
        self.assertEqual(after['budget']['calls'], before['budget']['calls'] + 2)
        self.assertEqual(after['budget']['tokens'], before['budget']['tokens'] + 200)
        final = self.store.snapshot()
        self.assertEqual(final['phase'], 'execution')
        self.assertEqual(final['architecture'], snapshot['architecture'])
        self.assertEqual(final['planning']['proposal']['coverage'], snapshot['planning']['proposal']['coverage'])
        self.assertTrue(final['planning']['roadmap']['gate']['passed'])
        with self.store._connection() as db:
            self.assertEqual([tuple(r) for r in db.execute('SELECT * FROM planning_calls ORDER BY id')][:len(history)], history)
        self.service.configure_execution(policy, self.verification, self.identifier, planning_recovery=request)
        self.assertEqual(len(self.jobs), jobs + 1)

    def test_operator_recovery_failure_stays_bounded_across_restarts_and_renamed_requests(self):
        _, before, policy, request = self.blocked_recovery()
        self.sdk.responses = [lambda c: deepcopy(c['proposal']), review()]
        self.service.configure_execution(policy, self.verification, self.identifier, planning_recovery=request)
        self.service.run_pending(*self.jobs[-1])
        calls = self.service.get_status(self.identifier)['continuation']['budget']['calls']
        self.assertEqual(calls, before['budget']['calls'] + 2)
        self.service.resume(self.identifier)
        self.service.configure_execution(policy, self.verification, self.identifier, planning_recovery=request)
        self.assertEqual(len(self.jobs), 2)
        self.assertEqual(self.store.snapshot()['planning']['stage'], 'blocked')
        renamed = {**request, 'request_id': 'different-name'}
        restarted = FactoryService(self.service.registry, launcher=lambda *args: self.fail('Unexpected worker'))
        with self.assertRaises(FactoryError) as error:
            restarted.configure_execution(policy, self.verification, self.identifier, planning_recovery=renamed)
        self.assertEqual(error.exception.code, 'planning_recovery_unavailable')

    def test_operator_recovery_rejects_stale_sources_changed_checks_permissions_and_call_budget(self):
        snapshot, before, policy, request = self.blocked_recovery()
        for defect in ('proposal', 'run', 'checks', 'model', 'calls', 'time'):
            p, v, r = deepcopy(policy), deepcopy(self.verification), deepcopy(request)
            if defect == 'proposal': r['proposal_fingerprint'] = 'sha256:old'
            if defect == 'run': r['run_id'] = 'another-run'
            if defect == 'checks': v['checks'][0]['min_tests'] = 1
            if defect == 'model': p['model'] = 'another-model'
            if defect == 'calls': p['continuation']['max_calls'] -= 1
            if defect == 'time': p['continuation']['max_seconds'] = 10
            with self.subTest(defect=defect), self.assertRaises(FactoryError):
                self.service.configure_execution(p, v, self.identifier, planning_recovery=r)
            self.assertEqual(self.store.snapshot(), snapshot)
        self.assertEqual(len(self.jobs), 1)

    def test_operator_budget_extension_records_delta_without_resetting_consumption(self):
        _, before, policy, request = self.blocked_recovery()
        policy['continuation']['max_calls'] += 4
        policy['continuation']['max_tokens'] += 150000
        with self.assertRaises(FactoryError):
            self.service.configure_execution(policy, self.verification, self.identifier)
        self.service.configure_execution(policy, self.verification, self.identifier, planning_recovery=request)
        after = self.service.get_status(self.identifier)['continuation']
        self.assertEqual(after['budget']['calls'], before['budget']['calls'])
        self.assertEqual(after['budget']['tokens'], before['budget']['tokens'])
        self.assertEqual(after['budget']['calls_remaining'], before['budget']['calls_remaining'] + 4)
        self.assertEqual(after['budget']['tokens_remaining'], before['budget']['tokens_remaining'] + 150000)
        self.assertEqual(after['created_at'], before['created_at'])
        self.assertEqual(after['deadline'], before['deadline'] + 1800)
        self.assertEqual(after['budget_amendments'][-1]['before'], before['limits'])
        self.assertEqual(after['budget_amendments'][-1]['after'], policy['continuation'])
        self.service.configure_execution(policy, self.verification, self.identifier, planning_recovery=request)
        self.assertEqual(len(self.service.get_status(self.identifier)['continuation']['budget_amendments']), 1)

    def test_operator_recovery_preserves_pause_and_recovers_crash_before_launch(self):
        _, before, policy, request = self.blocked_recovery()
        self.service.pause(self.identifier)
        self.service.configure_execution(policy, self.verification, self.identifier, planning_recovery=request)
        self.assertTrue(Runtime(self.store).paused())
        self.assertEqual(len(self.jobs), 1)
        conflict = {**request, 'reason': 'Changed meaning'}
        with self.assertRaises(FactoryError) as error:
            self.service.configure_execution(policy, self.verification, self.identifier, planning_recovery=conflict)
        self.assertEqual(error.exception.code, 'request_id_conflict')
        self.sdk.responses = [self.repaired_owner, review()]
        self.service.resume(self.identifier)
        self.service.run_pending(*self.jobs[-1])
        self.assertEqual(self.store.snapshot()['phase'], 'execution')
        self.assertEqual(self.jobs[-1][1], before['runtime_id'])

    def test_operator_recovery_commit_before_launch_is_recoverable_and_concurrent_pause_wins(self):
        _, before, policy, request = self.blocked_recovery()
        original = self.service.resume
        with patch.object(self.service, 'resume', side_effect=OSError('Crash before launch')):
            with self.assertRaises(OSError):
                self.service.configure_execution(policy, self.verification, self.identifier, planning_recovery=request)
        self.assertEqual(len(self.jobs), 1)
        def pause_before_resume(*args, **kwargs):
            self.service.pause(self.identifier)
            return original(*args, **kwargs)
        with patch.object(self.service, 'resume', side_effect=pause_before_resume):
            self.service.configure_execution(policy, self.verification, self.identifier, planning_recovery=request)
        self.assertTrue(Runtime(self.store).paused())
        self.assertEqual(len(self.jobs), 1)
        self.sdk.responses = [self.repaired_owner, review()]
        self.service.resume(self.identifier); self.service.run_pending(*self.jobs[-1])
        self.assertEqual(self.store.snapshot()['phase'], 'execution')
        self.assertEqual(self.service.get_status(self.identifier)['continuation']['deadline'], before['deadline'] + 1800)

    def test_completed_critic_checkpoint_recovers_at_limit_without_another_call(self):
        from factory.planning import Planning
        _, before, policy, request = self.blocked_recovery(schema_rejection=True)
        self.sdk.responses = [self.repaired_owner, review()]
        self.service.configure_execution(policy, self.verification, self.identifier, planning_recovery=request)
        accept = Planning._accept_response
        def crash(planner, state, *args, **kwargs):
            if state['stage'] == 'critic_final':
                raise OSError('Stopped after durable model output and before planning checkpoint')
            return accept(planner, state, *args, **kwargs)
        with patch.object(Planning, '_accept_response', crash), self.assertRaises(OSError):
            self.service.run_pending(*self.jobs[-1])
        state = self.store.snapshot()['planning']
        self.assertEqual(state['calls'], state['authorized_recovery']['call_limit'])
        contexts = len(self.sdk.contexts)
        self.service.resume(self.identifier); self.service.run_pending(*self.jobs[-1])
        self.assertEqual(self.store.snapshot()['phase'], 'execution')
        self.assertEqual(len(self.sdk.contexts), contexts)
        self.assertEqual(self.service.get_status(self.identifier)['continuation']['budget']['calls'], before['budget']['calls'] + 2)
        with self.store._connection() as db:
            call = db.execute('SELECT status,error FROM planning_calls ORDER BY id DESC LIMIT 1').fetchone()
        self.assertEqual(call['status'], 'completed')
        self.assertIn('before planning checkpoint', call['error'])

    def test_separate_operator_grants_keep_one_aggregate_budget_and_replay_history(self):
        from factory.architecture import fingerprint
        _, before, policy, first = self.blocked_recovery()
        finding = {'id':'acceptance_gap','severity':'high','category':'verification','targets':['s1'],
            'description':'The revised proposal still needs evidence','recommendation':'Preserve the criteria and resolve the gap'}
        self.sdk.responses = [self.repaired_owner, review([finding])]
        self.service.configure_execution(policy, self.verification, self.identifier, planning_recovery=first)
        self.service.run_pending(*self.jobs[-1])
        second = {**first,'request_id':'separately-authorized-correction',
            'proposal_fingerprint':fingerprint(self.store.snapshot()['planning']['proposal']),
            'reason':'Operator authorizes correcting the newly reviewed proposal within the original call/token ceilings'}
        self.sdk.responses = [lambda c: deepcopy(c['proposal']), review()]
        self.service.configure_execution(policy, self.verification, self.identifier, planning_recovery=second)
        self.service.run_pending(*self.jobs[-1])
        after = self.service.get_status(self.identifier)['continuation']
        self.assertEqual(after['budget']['calls'], before['budget']['calls'] + 4)
        self.assertEqual(after['limits']['max_calls'], before['limits']['max_calls'])
        self.assertEqual(after['limits']['max_tokens'], before['limits']['max_tokens'])
        self.assertEqual(after['deadline'], before['deadline'] + 1800)
        self.assertEqual(len(after['planning_recoveries']), 2)
        self.service.configure_execution(policy, self.verification, self.identifier, planning_recovery=first)
        self.service.configure_execution(policy, self.verification, self.identifier, planning_recovery=second)
        self.assertEqual(len(self.jobs), 3)

    def test_schema_rejection_recovery_retains_call_budget_without_inventing_usage(self):
        from factory.continuation_store import ContinuationStore
        provider_error = {'codex_error_info':'other','message':json.dumps({'status':400,'error':{
            'type':'invalid_request_error','code':'invalid_json_schema','param':'text.format.schema'}})}
        self.sdk.responses = [FactoryError('infrastructure_failed', str(provider_error)), complete_reply()]
        self.policy['continuation']['max_calls'] = 2
        self.start()
        with self.assertRaises(FactoryError): self.service.run_pending(*self.jobs[-1])
        before = ContinuationStore(self.store).inspect()
        self.assertEqual(before['budget']['usage_unknown_calls'], 1)
        self.service.resume(self.identifier)
        with self.assertRaises(FactoryError): self.service.run_pending(*self.jobs[-1])
        after = ContinuationStore(self.store).inspect()
        self.assertEqual(before['id'], after['id'])
        self.assertEqual(before['deadline'], after['deadline'])
        self.assertEqual(after['budget']['calls'], 2)
        self.assertEqual(after['budget']['tokens'], 100)
        self.assertEqual(after['budget']['usage_unknown_calls'], 0)
        self.assertEqual(after['budget']['rejected_before_inference'], 1)
        with self.store._connection() as db:
            record=json.loads(db.execute('SELECT data FROM continuation_calls ORDER BY rowid LIMIT 1').fetchone()[0])
            self.assertIsNone(record['usage'])
            self.assertEqual(record['error']['message'], str(provider_error))

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

    def test_resume_after_binding_reuses_original_analysis_run(self):
        self.start(); self.service.run_pending(*self.jobs[-1])
        before = self.service.get_status(self.identifier)['autonomous_run']['run_id']
        definition = deepcopy(self.verification)
        for check in definition['checks']:
            check.update(gate='milestone_gate', criteria=[0, 1])
        self.service.configure_execution(self.policy, definition, self.identifier)
        status = self.service.resume(self.identifier)
        self.assertEqual(status['autonomous_run']['run_id'], before)
        self.assertEqual(self.jobs[-1][1], before)
        self.assertEqual(len(self.jobs), 2)
        self.service.resume(self.identifier)
        self.assertEqual(len(self.jobs), 2)

    def test_missing_usage_blocks_the_next_phase_without_an_extra_inference(self):
        self.sdk.usage = None
        self.start()
        with self.assertRaises(FactoryError) as exc:
            self.service.run_pending(*self.jobs[-1])
        self.assertEqual(exc.exception.code, 'usage_unknown')
        self.assertEqual(len(self.sdk.contexts), 1)

    def test_binding_extension_records_delta_without_resetting_run_or_consumption(self):
        self.start(); self.service.run_pending(*self.jobs[-1])
        from factory.continuation_store import ContinuationStore
        before = ContinuationStore(self.store).latest()
        definition = deepcopy(self.verification)
        for check in definition['checks']:
            check.update(gate='milestone_gate', criteria=[0, 1])
        self.policy['continuation'].update(max_slices=3, max_seconds=3600, max_calls=30, max_tokens=500000)
        self.service.configure_execution(self.policy, definition, self.identifier)
        after = ContinuationStore(self.store).latest()
        self.assertEqual(after['deadline'], before['deadline'] + 1800)
        self.assertEqual(after['created_at'], before['created_at'])
        self.assertEqual(after['runtime_id'], before['runtime_id'])
        self.assertEqual(after['budget_amendments'][0]['before'], before['limits'])
        self.assertEqual(self.service.get_status(self.identifier)['continuation']['budget']['analysis_calls'], 5)
        with self.assertRaises(FactoryError):
            self.service.configure_execution(self.policy, definition, self.identifier)

    def test_prior_input_exclusions_require_exact_completed_preplanning_input(self):
        from factory.project_validation import prior_input_authorizations
        self.start(); self.service.run_pending(*self.jobs[-1])
        snapshot = self.store.snapshot()
        snapshot['planning']['roadmap']['plan']['coverage'][0].update(disposition='out_of_scope', rationale='Explicit pilot scope')
        key = snapshot['planning']['roadmap']['plan']['coverage'][0]['requirement']
        item = {'requirement': key, 'prior_input': {'request_id': 'analysis-once', 'quote': 'No quiero UI, red'}}
        definition = {'project_acceptance': {'exclusions': [item]}}
        proofs = prior_input_authorizations(self.store, snapshot, definition)
        self.assertEqual(proofs[0]['turn_id'], 1)
        self.assertEqual(self.store.snapshot()['decisions'], [])
        item['prior_input']['quote'] = 'Fabricated permission'
        with self.assertRaises(FactoryError):
            prior_input_authorizations(self.store, snapshot, definition)
        item['prior_input']['quote'] = 'No quiero UI, red'
        snapshot['planning']['roadmap']['created_at'] = '2000-01-01T00:00:00.000Z'
        with self.assertRaises(FactoryError):
            prior_input_authorizations(self.store, snapshot, definition)

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
        self.service = FactoryService(self.registry, workflow_mode='verified', discovery_model=self.discovery, architecture_model=self.architect,
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
