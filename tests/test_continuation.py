import json
from pathlib import Path
import tempfile
import time
from copy import deepcopy
import unittest
from unittest.mock import patch

from factory.continuation import Continuation
from factory.continuation_store import ContinuationStore
from factory.execution_store import ExecutionStore
from factory.registry import FactoryError
from factory.runtime import Runtime
from tests.continuation_fakes import setup, implementation_a, implementation_b, implementation_c, refinement


class ContinuationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def product(self, **options):
        self.service, self.store, self.jobs, self.git, self.sdk, self.refiner = setup(self.root, **options)
        self.groups, self.executions = ContinuationStore(self.store), ExecutionStore(self.store)

    def run_product(self):
        self.service.execute_next_slice(request_id='pilot')
        self.service.run_pending(*self.jobs[-1])
        return self.groups.inspect()

    def test_three_slices_refine_from_real_accepted_code_and_stop_at_milestone(self):
        self.product(harness=True)
        before = self.store.snapshot(); head = self.git('rev-parse', 'HEAD')
        data = self.run_product()
        self.assertEqual(data['state'], 'validation_pending', data)
        self.assertEqual([a['slice_id'] for a in data['accepted']], ['s1', 's2', 's3'])
        self.assertEqual(len(self.sdk.contexts), 3)
        self.assertEqual(len(self.refiner.contexts), 2)
        self.assertIn('return a + b', self.refiner.contexts[0]['files']['app.py'])
        self.assertEqual(self.refiner.contexts[0]['dependency_evidence'][0]['slice_id'], 's1')
        self.assertIn('app.py', self.sdk.contexts[1]['files'])
        receipts = self.executions.acceptances()
        self.assertEqual(receipts[1]['base_commit'], receipts[0]['commit'])
        self.assertEqual(receipts[2]['base_commit'], receipts[1]['commit'])
        self.assertEqual(self.git('rev-parse', 'factory/accepted'), receipts[-1]['commit'])
        self.assertTrue(all(e['status'] == 'PASS' for a in receipts for e in a['evidence']))
        self.assertEqual(self.git('rev-parse', 'HEAD'), head)
        after = self.store.snapshot()
        self.assertEqual(after['planning']['roadmap'], before['planning']['roadmap'])
        self.assertEqual(after['discovery']['knowledge'], before['discovery']['knowledge'])
        self.assertEqual(after['planning']['roadmap']['plan']['milestones'][0]['status'], 'open')
        self.assertEqual(after['phase'], 'execution')
        self.assertEqual(data['budget']['calls'], 5)
        self.assertEqual(data['budget']['implementation_tokens'], 300)
        self.assertEqual(data['budget']['refinement_tokens'], 200)
        self.assertTrue(data['pending_gates'])
        self.assertTrue(all(g['status'] == 'NOT_RUN' for g in data['pending_gates']))
        self.service.execute_next_slice(request_id='new-client-after-milestone')
        self.assertEqual(len(self.jobs), 1)

    def test_ready_slices_do_not_call_refiner(self):
        self.product(ready=True)
        self.assertEqual(self.run_product()['state'], 'validation_pending')
        self.assertEqual(self.refiner.contexts, [])

    def test_repair_b_does_not_repeat_a(self):
        self.product()
        self.sdk.responses = [implementation_a(), implementation_b(True), implementation_b(), implementation_c()]
        data = self.run_product()
        self.assertEqual(data['state'], 'validation_pending', data)
        self.assertEqual(len(self.sdk.contexts), 4)
        self.assertEqual(self.sdk.contexts[2]['failure']['kind'], 'code_failure')
        self.assertEqual(len(self.executions.acceptances()), 3)

    def test_configurable_slice_checkpoint_and_duplicate_intents(self):
        self.product()
        self.assertEqual(self.service.get_status()['capabilities']['execution_slice_limit'], 3)
        self.assertTrue(self.service.inspect(view='execution')['policy']['continuation']['enabled'])
        policy = self.groups.latest()
        journal = self.executions
        configured = {k: v for k, v in journal.policy().items() if k not in ('repository', 'definition_id', 'authorized_at')}
        configured['continuation']['max_slices'] = 2
        self.service.configure_execution(configured, journal.definition(journal.policy()['definition_id'])['verification'])
        first = self.service.execute_next_slice(request_id='pilot')
        duplicate = self.service.execute_next_slice(request_id='another-client')
        self.assertEqual(first['continuation']['id'], duplicate['continuation']['id'])
        self.assertEqual(len(self.jobs), 1)
        self.service.run_pending(*self.jobs[0])
        self.assertEqual(self.groups.latest()['state'], 'checkpoint')
        self.service.execute_next_slice(request_id='pilot')
        self.service.resume()
        self.assertEqual(len(self.jobs), 1)
        self.assertEqual(len(self.executions.acceptances()), 2)

    def test_aggregate_calls_survive_resume(self):
        self.product()
        configured = {k: v for k, v in self.executions.policy().items() if k not in ('repository', 'definition_id', 'authorized_at')}
        configured['continuation']['max_calls'] = 2
        self.service.configure_execution(configured, self.executions.definition(self.executions.policy()['definition_id'])['verification'])
        data = self.run_product()
        self.assertEqual(data['state'], 'budget_exhausted', data)
        identifier, deadline = data['id'], data['deadline']
        self.service.resume(); self.service.run_pending(*self.jobs[-1])
        data = self.groups.inspect()
        self.assertEqual((data['id'], data['deadline'], data['budget']['calls']), (identifier, deadline, 2))
        self.assertEqual(len(self.sdk.contexts), 1)

    def test_invalid_refinement_has_only_one_correction_across_restarts(self):
        self.product()
        self.refiner.responses = [{}, {}]
        data = self.run_product()
        self.assertEqual(data['diagnostic']['code'], 'refinement_exhausted', data)
        self.service.resume(); self.service.run_pending(*self.jobs[-1])
        self.assertEqual(len(self.refiner.contexts), 2)
        self.assertEqual(len(self.executions.acceptances()), 1)

    def test_restart_after_a_publication_does_not_rebuild_it(self):
        self.product()
        original = Continuation.choose
        def crash(controller, data, snapshot):
            if data['accepted']:
                raise RuntimeError('Simulated process death before next dispatch')
            return original(controller, data, snapshot)
        with patch.object(Continuation, 'choose', crash), self.assertRaises(RuntimeError):
            self.run_product()
        self.assertEqual(len(self.executions.acceptances()), 1)
        first = self.executions.acceptances()[0]['commit']
        self.service.resume(); self.service.run_pending(*self.jobs[-1])
        self.assertEqual(self.groups.latest()['state'], 'validation_pending')
        self.assertEqual(self.executions.acceptances()[0]['commit'], first)
        self.assertEqual(len(self.sdk.contexts), 3)

    def test_refinement_pause_retains_response_and_resumes_same_run(self):
        self.product()
        self.refiner.on_call = lambda stop: self.service.pause()
        data = self.run_product()
        self.assertEqual(data['state'], 'paused', data)
        self.refiner.on_call = None
        identifier = data['id']
        self.service.resume(); self.service.run_pending(*self.jobs[-1])
        self.assertEqual(self.groups.latest()['id'], identifier)
        self.assertEqual(self.groups.latest()['state'], 'validation_pending')
        self.assertEqual(len(self.refiner.contexts), 2)

    def test_refinement_question_answer_continues_without_extra_continue(self):
        self.product(count=2)
        def question(context):
            p = refinement(context); p['questions'] = ['Choose the implementation detail: direct composition?']
            return p
        self.refiner.responses = [question, refinement]
        self.assertEqual(self.run_product()['state'], 'waiting_decision')
        decision = self.store.snapshot()['decisions'][-1]
        self.service.answer_decision(decision['id'], 'Direct composition')
        self.assertEqual(len(self.jobs), 2)
        self.service.run_pending(*self.jobs[-1])
        data = self.groups.latest()
        self.assertEqual(data['state'], 'validation_pending', data)

    def configure(self, *, limits=None, verification=None):
        policy = {k: v for k, v in self.executions.policy().items() if k not in ('repository', 'definition_id', 'authorized_at')}
        policy['continuation'].update(limits or {})
        definition = self.executions.definition(self.executions.policy()['definition_id'])['verification']
        self.service.configure_execution(policy, verification or definition)

    def test_pause_during_implementation_retains_response_and_budget(self):
        self.product(count=2)
        states = []
        def pause(stop):
            states.append(self.service.pause()['state'])
            self.assertEqual(stop(), 'paused')
        self.sdk.on_call = pause
        data = self.run_product()
        self.assertEqual(states, ['pause_requested'])
        self.assertEqual(data['state'], 'paused')
        self.assertEqual(data['budget']['calls'], 1)
        self.sdk.on_call = None
        self.service.resume(); self.service.run_pending(*self.jobs[-1])
        self.assertEqual(self.groups.latest()['id'], data['id'])
        self.assertEqual(self.groups.latest()['deadline'], data['deadline'])
        self.assertEqual(self.groups.latest()['state'], 'validation_pending')
        self.assertEqual(len(self.sdk.contexts), 2)

    def test_tokens_and_elapsed_duration_are_aggregate_and_persistent(self):
        self.product(count=2)
        self.configure(limits={'max_tokens': 150})
        data = self.run_product()
        self.assertEqual(data['state'], 'budget_exhausted', data)
        self.assertEqual(data['budget']['tokens'], 200)
        self.assertEqual(len(self.sdk.contexts), 1)
        self.service.resume(); self.service.run_pending(*self.jobs[-1])
        self.assertEqual(self.groups.inspect()['budget']['calls'], 2)
        data = self.groups.latest(); data['deadline'] = time.time() - 1
        self.groups.save(data)
        self.service.resume(); self.service.run_pending(*self.jobs[-1])
        self.assertEqual(self.groups.inspect()['budget']['seconds_remaining'], 0)
        self.assertEqual(len(self.refiner.contexts), 1)

    def test_git_ref_updated_sqlite_interrupted_reconciles_without_reimplementing_a(self):
        self.product(count=2)
        import factory.integrated_code as module
        original = module.git
        def crash(project, *args):
            value = original(project, *args)
            if args[0] == 'update-ref' and len(self.executions.acceptances()) == 1:
                raise RuntimeError('Crash after accepted ref CAS before SQLite head update')
            return value
        with patch.object(module, 'git', crash), self.assertRaises(RuntimeError):
            self.run_product()
        receipt = self.executions.acceptances()[0]
        self.assertEqual(self.git('rev-parse', 'factory/accepted'), receipt['commit'])
        with self.store._connection() as db:
            self.assertIsNone(json.loads(db.execute('SELECT data FROM integrated_code').fetchone()[0])['execution_id'])
        self.service.resume(); self.service.run_pending(*self.jobs[-1])
        self.assertEqual(self.groups.latest()['state'], 'validation_pending')
        self.assertEqual(len(self.sdk.contexts), 2)
        self.assertEqual(self.executions.acceptances()[1]['base_commit'], receipt['commit'])

    def test_stale_refinement_source_cannot_publish_and_preserves_a(self):
        self.product(count=2)
        real = self.store.snapshot
        changed = []
        def snapshot():
            value = real()
            if changed:
                value['planning']['roadmap']['plan']['objective'] = 'Changed without compatible revision'
            return value
        self.refiner.on_call = lambda stop: changed.append(True)
        with patch.object(type(self.store), 'snapshot', lambda store: snapshot() if store.project == self.store.project else real()):
            # Avoid recursively calling the patched bound method: real retains its function.
            data = self.run_product()
        self.assertEqual(data['diagnostic']['code'], 'stale_sources', data)
        self.assertEqual(len(self.executions.acceptances()), 1)
        with self.store._connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM refinement_revisions').fetchone()[0], 0)

    def test_integration_gate_failure_prevents_refinement_and_implementation(self):
        def customize(plan):
            gate = deepcopy(plan['gates'][0])
            gate.update(id='integration_b', kind='integration', target='s2', trigger='before_slice',
                        signals=['cross_component'], cost='moderate', checks=['Check accepted dependency integration'])
            plan['gates'].append(gate)
            plan['slices'][1]['verification_triggers'] = ['cross_component']
            plan['harness'][0]['needed_by_gates'].append('integration_b')
        self.product(count=2, custom=customize)
        definition = self.executions.definition(self.executions.policy()['definition_id'])['verification']
        check = deepcopy(definition['checks'][0]); check.update(id='integration', gate='integration_b',
            cases=[{'args_json': '[1,2]', 'expected_json': '999'}])
        definition['checks'].append(check)
        self.configure(verification=definition)
        data = self.run_product()
        self.assertEqual(data['diagnostic']['code'], 'gate_pending', data)
        self.assertEqual(data['diagnostic']['checks'][0]['status'], 'FAIL')
        self.assertEqual(len(self.sdk.contexts), 1)
        self.assertEqual(self.refiner.contexts, [])

    def test_required_specialist_blocks_b_before_refinement(self):
        self.product(count=2)
        definition = self.executions.definition(self.executions.policy()['definition_id'])['verification']
        check = next(c for c in definition['checks'] if c['id'] == 'behavior_s2')
        check.update(kind='specialist', cases=[])
        self.configure(verification=definition)
        data = self.run_product()
        self.assertEqual(data['diagnostic']['code'], 'capability_unavailable', data)
        self.assertEqual(len(self.sdk.contexts), 1)
        self.assertEqual(len(self.executions.acceptances()), 1)
        self.assertEqual(self.refiner.contexts, [])

    def test_unknown_quota_in_refinement_stops_without_new_model_call(self):
        self.product(count=2)
        self.refiner.quota_value = {'observed_at': time.time(), 'buckets': {}}
        data = self.run_product()
        self.assertEqual(data['state'], 'quota_blocked', data)
        self.assertEqual(len(self.sdk.contexts), 1)
        self.assertEqual(self.refiner.contexts, [])
        self.assertEqual(data['budget']['calls'], 1)

    def test_required_skill_unavailable_for_refinement_has_durable_cause(self):
        self.product(count=2)
        from factory.skill_router import Policy, SkillRouter
        from factory.skill_catalog import Catalog
        self.sdk.on_call = lambda stop: setattr(self.service, 'router', SkillRouter(Catalog(), Policy(required=('absent',))))
        data = self.run_product()
        self.assertEqual(data['state'], 'blocked', data)
        self.assertEqual(data['diagnostic']['code'], 'skill_unavailable')
        self.assertEqual(self.refiner.contexts, [])
        self.assertEqual(len(self.executions.acceptances()), 1)

    def test_real_smoke_fixture_predeclares_two_slices_and_solution_is_absent(self):
        from scripts.execution_smoke_fixture import prepare, INITIAL_CODE
        from factory.workflow import Store
        from factory.execution_contract import validate_verification
        prepared = prepare(self.root, 'test-model', 'low', continuation=True)
        store = Store(prepared['project']['path'])
        plan = store.snapshot()['planning']['roadmap']['plan']
        self.assertEqual([s['maturity'] for s in plan['slices']], ['execution_ready', 'outline'])
        self.assertEqual(plan['slices'][1]['dependencies'], ['s1'])
        validate_verification(prepared['verification'], plan)
        self.assertEqual((store.project / 'records.py').read_text(), INITIAL_CODE)
        self.assertFalse((store.project / 'category_report.py').exists())
        self.assertTrue((store.project / 'test_category_report.py').exists())

    def test_from_discovery_pilot_has_independent_oracles_and_no_preloaded_phases(self):
        from scripts.execution_smoke_fixture import prepare_from_discovery
        from scripts.smoke_continuation import load_discovery_pilot
        from factory.workflow import Store
        root = self.root / 'persistent-pilot'
        value = prepare_from_discovery(root, 'gpt-5.6-terra', 'low', registry_home=self.root / 'registry')
        store = Store(value['project']['path'])
        snapshot = store.snapshot()
        self.assertEqual(snapshot['phase'], 'discovery')
        self.assertEqual(snapshot['discovery']['knowledge'], [])
        self.assertIsNone(snapshot['architecture']['baseline'])
        self.assertIsNone(snapshot['planning']['roadmap'])
        self.assertIsNone(ContinuationStore(store).latest())
        self.assertTrue(Runtime(store).paused())
        self.assertFalse((store.project / 'records.py').exists())
        self.assertFalse((store.project / 'category_report.py').exists())
        self.assertEqual(value['contract']['required_milestones'], 2)
        self.assertEqual(value['policy']['quota_reserve_percent'], 25)
        self.assertEqual(value['full_workflow_limits'], {'max_calls': 4, 'max_seconds': 300, 'max_tokens': 30000})
        self.assertFalse(value['full_workflow_limits_enforced'])
        self.assertEqual(load_discovery_pilot(root / 'report.json')['project'], value['project'])
        (store.project / 'user-change.txt').write_text('preserve')
        with self.assertRaises(FactoryError) as error:
            prepare_from_discovery(root, 'gpt-5.6-terra', 'low', registry_home=self.root / 'registry')
        self.assertEqual(error.exception.code, 'pilot_exists')
        self.assertEqual((store.project / 'user-change.txt').read_text(), 'preserve')
        (store.project / 'pilot-contract.json').write_text('{}')
        with self.assertRaises(FactoryError) as error:
            load_discovery_pilot(root / 'report.json')
        self.assertEqual(error.exception.code, 'pilot_contract_changed')

    def test_harness_file_without_accepted_capability_evidence_is_not_reused(self):
        self.product(count=2)
        acceptances = self.executions.acceptances
        def unproven(journal):
            values = acceptances()
            for a in values:
                a['harness_revision'] = None
            return values
        with patch.object(ExecutionStore, 'acceptances', unproven):
            data = self.run_product()
        self.assertEqual(data['diagnostic']['code'], 'harness_evidence_pending', data)
        self.assertEqual(self.refiner.contexts, [])

    def test_structural_refinement_proposal_is_retained_without_architect_call(self):
        self.product(count=2)
        def proposal(context):
            p = refinement(context)
            p['proposal'] = {'kind': 'architecture', 'reason': 'Requires new persistence', 'references': ['app']}
            return p
        self.refiner.responses = [proposal]
        data = self.run_product()
        self.assertEqual(data['diagnostic']['code'], 'refinement_proposal', data)
        self.service.resume(); self.service.run_pending(*self.jobs[-1])
        self.assertEqual(len(self.refiner.contexts), 1)
        self.assertEqual(len(self.sdk.contexts), 1)

    def test_relevance_key_ignores_other_files_and_detects_related_code_change(self):
        self.product(count=2)
        self.configure(limits={'max_calls': 2})
        self.run_product()
        group = self.groups.latest()
        from factory.refinement import Refiner
        with self.store._connection() as db:
            data = json.loads(db.execute('SELECT data FROM refinements').fetchone()[0])
        slice_ = self.store.snapshot()['planning']['roadmap']['plan']['slices'][1]
        unit = Refiner(Continuation(self.service, self.store), group, slice_,
                       self.executions.definition(self.executions.policy()['definition_id'])['verification'])
        key = unit.context(data)[1]
        (Path(data['worktree']) / 'other_component.py').write_text('value = 44\n')
        self.assertEqual(unit.context(data)[1], key)
        (Path(data['worktree']) / 'app.py').write_text('def add(a, b):\n    return a - b\n')
        self.assertNotEqual(unit.context(data)[1], key)
        self.service.resume(); self.service.run_pending(*self.jobs[-1])
        self.assertEqual(len(self.refiner.contexts), 1)  # An input change does not reset the run budget.

    def test_superseded_runtime_cannot_save_group_or_reserve_call(self):
        self.product()
        self.service.execute_next_slice(request_id='old')
        group = self.groups.latest()
        Runtime(self.store).queue()
        with self.assertRaisesRegex(FactoryError, 'superseded'):
            self.groups.save(group)
        from factory.continuation_store import reserve_call
        with self.store._connection(write=True) as db, self.assertRaisesRegex(FactoryError, 'ownership'):
            reserve_call(db, {'id': 'unit', 'continuation_id': group['id'], 'run_id': group['runtime_id']}, {'id': 'stale'})

    def test_single_slice_authorization_is_not_expanded_by_migration(self):
        self.product(ready=True)
        policy = {k: v for k, v in self.executions.policy().items() if k not in ('repository', 'definition_id', 'authorized_at', 'continuation')}
        definition = self.executions.definition(self.executions.policy()['definition_id'])['verification']
        self.service.configure_execution(policy, definition)
        self.store.initialize()
        self.run_product()
        self.assertIsNone(self.groups.latest())
        self.assertEqual(len(self.executions.acceptances()), 1)
        self.assertEqual(len(self.sdk.contexts), 1)

    def test_milestone_closure_does_not_dispatch_second_milestone(self):
        def customize(plan):
            mid = plan['milestones'][0]['id']
            moved = plan['slices'][2]['requirements'][-1]
            second = deepcopy(plan['milestones'][0]); second.update(id='m2', requirements=[moved], dependencies=[mid], risks=[], verification_gates=['close_m2'])
            plan['milestones'].append(second)
            plan['milestones'][0]['requirements'].remove(moved)
            for s in plan['slices'][:2]:
                s['requirements'].remove(moved)
            plan['slices'][2].update(milestone='m2', requirements=[moved])
            for c in plan['coverage']:
                if c['requirement'] == moved:
                    c.update(milestone='m2', slices=['s3'])
                else:
                    c['slices'].remove('s3')
            gate = deepcopy(next(g for g in plan['gates'] if g['kind'] == 'milestone'))
            gate.update(id='close_m2', target='m2', harness=[])
            plan['gates'].append(gate)
        self.product(custom=customize)
        data = self.run_product()
        self.assertEqual(data['state'], 'validation_pending', data)
        self.assertEqual([a['slice_id'] for a in data['accepted']], ['s1', 's2'])
        self.assertEqual(len(self.sdk.contexts), 2)
        self.assertTrue(all(m['status'] == 'open' for m in self.store.snapshot()['planning']['roadmap']['plan']['milestones']))
