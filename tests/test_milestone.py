import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from factory.continuation import Continuation
from factory.continuation_store import ContinuationStore
from factory.execution_store import ExecutionStore
from factory.milestone import MilestoneGate
from factory.milestone_store import MilestoneStore
from factory.registry import FactoryError
from factory.runtime import Runtime
from factory.verification import Verifier
from tests.milestone_fakes import setup_milestones
from tests.continuation_fakes import implementation_a, implementation_b, implementation_c


class MilestoneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def product(self, **kwargs):
        self.service, self.store, self.jobs, self.git, self.sdk, self.refiner = setup_milestones(self.root, **kwargs)
        self.groups = ContinuationStore(self.store)
        self.journal = MilestoneStore(self.store)
        self.executions = ExecutionStore(self.store)

    def run_product(self):
        self.service.execute_next_slice(request_id='milestones-once')
        self.service.run_pending(*self.jobs[-1])
        return self.groups.inspect()

    def configure(self, change):
        policy = {k: v for k, v in self.executions.policy().items() if k not in ('repository', 'definition_id', 'authorized_at')}
        definition = self.executions.definition(self.executions.policy()['definition_id'])['verification']
        change(policy, definition)
        self.service.configure_execution(policy, definition)

    def resume(self):
        self.service.resume(); self.service.run_pending(*self.jobs[-1])
        return self.groups.inspect()

    def test_end_to_end_integration_remediation_closure_outline_and_automatic_next_slice(self):
        self.product()
        snapshot, head = self.store.snapshot(), self.git('rev-parse', 'HEAD')
        data = self.run_product()
        self.assertEqual(data['state'], 'project_ready_for_validation', data)
        receipts = self.journal.receipts()
        self.assertEqual([r['milestone'] for r in receipts], ['m1', 'm2'])
        history = self.journal.issues()[0]
        self.assertEqual(history['history'][0]['evidence']['status'], 'FAIL')
        self.assertIn('AssertionError', history['history'][0]['evidence']['log'])
        self.assertEqual((history['attempts'], history['state']), (1, 'resolved'))
        self.assertEqual([r['state'] for r in self.journal.rows('milestone_validations')], ['failed', 'verified', 'verified'])
        self.assertEqual(len(self.sdk.contexts), 4)
        self.assertEqual(self.sdk.contexts[2]['failure']['kind'], 'milestone_integration_failure')
        context = self.refiner.contexts[0]
        self.assertEqual(context['milestone_preparation']['milestone']['id'], 'm2')
        self.assertEqual(context['milestone_preparation']['accepted_milestones'][0]['commit'], receipts[0]['commit'])
        self.assertIn('return a + b', context['files']['app.py'])
        self.assertEqual(data['budget']['preparation_calls'], 1)
        self.assertEqual(data['budget']['remediation_calls'], 1)
        self.assertEqual(data['budget']['work_units'], 4)
        self.assertEqual(data['budget']['calls'], 5)
        accepted = self.executions.acceptances()
        self.assertTrue(all(b['base_commit'] == a['commit'] for a, b in zip(accepted, accepted[1:])))
        self.assertEqual(receipts[0]['commit'], accepted[2]['commit'])
        self.assertEqual(self.git('rev-parse', 'factory/accepted'), accepted[-1]['commit'])
        self.assertEqual(self.git('rev-parse', 'HEAD'), head)
        self.assertEqual(self.store.snapshot()['planning']['roadmap'], snapshot['planning']['roadmap'])
        self.assertEqual(self.store.snapshot()['phase'], 'execution')
        self.assertTrue(all(r['progress']['status'] == 'active' for r in self.service.inspect(view='requirements')['requirements']))
        self.assertEqual(len(self.jobs), 1)

    def test_empty_outline_develops_only_approved_gate_obligations(self):
        self.product(empty_outline=True)
        data = self.run_product()
        self.assertEqual(data['state'], 'project_ready_for_validation', data)
        context = self.refiner.contexts[0]
        self.assertTrue(context['slice']['acceptance_criteria'])
        self.assertEqual(self.store.snapshot()['planning']['roadmap']['plan']['slices'][2]['acceptance_criteria'], [])

    def test_inter_milestone_disabled_by_default_and_cross_cutting_requirement_stays_active(self):
        self.product()
        self.configure(lambda p, d: p['continuation'].pop('inter_milestone'))
        data = self.run_product()
        self.assertEqual(data['state'], 'milestone_closed', data)
        self.assertEqual(data['next_milestone'], 'm2')
        self.assertEqual(self.refiner.contexts, [])
        requirement = self.service.inspect(view='requirements')['requirements'][0]
        self.assertEqual(requirement['progress']['status'], 'active')
        self.assertEqual(requirement['progress']['owner'], 'm1')
        self.assertEqual([c['status'] for c in requirement['progress']['contributions']], ['verified_contribution', 'pending'])

    def test_three_units_remains_three_including_remediation(self):
        self.product(limit=3)
        data = self.run_product()
        self.assertEqual(data['state'], 'checkpoint', data)
        self.assertEqual(data['budget']['units_remaining'], 0)
        self.assertEqual(data['budget']['remediations'], 1)
        self.assertEqual(self.refiner.contexts, [])
        self.assertEqual(len(self.journal.receipts()), 1)
        self.assertEqual(self.resume()['id'], data['id'])
        self.assertEqual(len(self.sdk.contexts), 3)

    def test_restart_after_close_before_preparation_preserves_receipt_and_budget(self):
        self.product()
        with patch.object(Continuation, 'advance', side_effect=RuntimeError('death after receipt')), self.assertRaises(RuntimeError):
            self.run_product()
        receipt = self.journal.receipts()[0]
        previous = self.groups.inspect()
        data = self.resume()
        self.assertEqual(data['state'], 'project_ready_for_validation', data)
        self.assertEqual((data['id'], data['deadline']), (previous['id'], previous['deadline']))
        self.assertEqual(self.journal.receipts()[0], receipt)
        self.assertEqual(data['budget']['calls'], 5)
        self.assertEqual(len(self.sdk.contexts), 4)

    def test_restart_after_verification_does_not_rerun_current_checks_and_receipt_idempotent(self):
        self.product(inter=False)
        with patch.object(MilestoneGate, 'publish', side_effect=RuntimeError('death after verified')), self.assertRaises(RuntimeError):
            self.run_product()
        with patch.object(Verifier, 'run', side_effect=AssertionError('must reuse exact saved evidence')):
            data = self.resume()
        self.assertEqual(data['state'], 'milestone_closed', data)
        receipt = self.journal.receipts()[0]
        self.service.execute_next_slice(request_id='milestones-once')
        self.service.resume()
        self.assertEqual(self.journal.receipts(), [receipt])
        with self.store._connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM factory_control_events WHERE kind='milestone_closed'").fetchone()[0], 1)

    def test_missing_closure_check_never_passes(self):
        self.product()
        self.configure(lambda p, d: d['checks'].remove(next(c for c in d['checks'] if c['id'] == 'integrated')))
        data = self.run_product()
        self.assertEqual(data['state'], 'validation_pending', data)
        self.assertEqual(self.journal.receipts(), [])
        self.assertEqual(len(self.sdk.contexts), 2)
        self.assertIn('milestone_criteria_unmapped', data['diagnostic']['pending'])

    def test_unsupported_system_checkpoint_blocks_without_remediation(self):
        def custom(plan):
            gate = dict(next(g for g in plan['gates'] if g['id'] == 'milestone_gate'))
            gate.update(id='system', kind='system', trigger='project_checkpoint', checks=['Required browser checkpoint'], harness=[])
            plan['gates'].append(gate)
        self.product(custom=custom)
        def configure(p, d):
            check = dict(d['checks'][-1]); check.update(id='system_check', gate='system', kind='specialist', target='browser', cases=[], criteria=[])
            d['checks'].append(check)
        self.configure(configure)
        data = self.run_product()
        self.assertEqual(data['state'], 'blocked', data)
        self.assertEqual(data['diagnostic']['classification'], 'unsupported_validation')
        self.assertEqual(len(self.sdk.contexts), 2)
        self.assertEqual(self.journal.receipts(), [])

    def test_external_accepted_ref_change_before_close_invalidates_evidence(self):
        self.product(inter=False)
        original = MilestoneGate.publish
        def changed(gate, validation, checks):
            self.git('update-ref', 'refs/heads/factory/accepted', self.git('rev-parse', 'HEAD'))
            return original(gate, validation, checks)
        with patch.object(MilestoneGate, 'publish', changed):
            data = self.run_product()
        self.assertEqual(data['diagnostic']['code'], 'integration_conflict', data)
        self.assertEqual(self.journal.receipts(), [])

    def test_sources_change_between_verification_and_close_cannot_weaken_criteria(self):
        self.product(inter=False)
        original = MilestoneGate.publish
        def changed(gate, validation, checks):
            snapshot = gate.store.snapshot
            def weakened(store):
                value = snapshot()
                value['planning']['roadmap']['plan']['milestones'][0]['closure_conditions'] = []
                return value
            with patch.object(type(self.store), 'snapshot', weakened):
                return original(gate, validation, checks)
        with patch.object(MilestoneGate, 'publish', changed):
            data = self.run_product()
        self.assertEqual(data['diagnostic']['code'], 'stale_sources', data)
        self.assertEqual(self.journal.receipts(), [])

    def test_remediation_cannot_rewrite_frozen_oracle(self):
        self.product()
        self.configure(lambda p, d: p['write_paths'].append('test_integrated.py'))
        self.sdk.responses[2]['changes'] = [{'path': 'test_integrated.py', 'operation': 'write', 'content': 'pass\n'}]
        data = self.run_product()
        self.assertIn(data['diagnostic']['code'], ('verification_weakened', 'scope_violation'), data)
        self.assertEqual(len(self.executions.acceptances()), 2)
        self.assertEqual(self.journal.receipts(), [])
        with self.assertRaisesRegex(FactoryError, 'frozen'):
            self.configure(lambda p, d: d['checks'].clear())

    def test_remediation_exhaustion_survives_restart_and_changed_failure_message(self):
        self.product()
        self.configure(lambda p, d: p.update(max_attempts=1))
        self.sdk.responses[2]['changes'] = [{'path': 'app.py', 'operation': 'write',
            'content': 'def add(a, b):\n    return max(a, 0) + b # different text, same defect\n'}]
        data = self.run_product()
        self.assertEqual(data['state'], 'budget_exhausted', data)
        self.assertEqual(self.journal.issues()[0]['attempts'], 1)
        self.assertEqual(len(self.sdk.contexts), 3)
        again = self.resume()
        self.assertEqual(again['budget']['remediation_calls'], 1)
        self.assertEqual(len(self.sdk.contexts), 3)
        self.assertEqual(self.journal.receipts(), [])

    def test_validation_does_not_need_quota_or_remaining_model_calls(self):
        self.product(inter=False)
        self.sdk.responses = [implementation_a(), implementation_b()]
        self.configure(lambda p, d: p['continuation'].update(max_calls=2))
        original = MilestoneGate.run
        def without_model(gate):
            self.sdk.init_error = AssertionError('Local validation must not initialize a model')
            return original(gate)
        with patch.object(MilestoneGate, 'run', without_model):
            data = self.run_product()
        self.assertEqual(data['state'], 'milestone_closed', data)
        self.assertEqual(data['budget']['calls_remaining'], 0)
        self.assertEqual(len(self.sdk.contexts), 2)

    def test_duration_and_pause_prevent_publication_and_keep_verified_evidence(self):
        self.product(inter=False)
        original = MilestoneGate.publish
        def paused(gate, validation, checks):
            self.service.pause()
            return original(gate, validation, checks)
        with patch.object(MilestoneGate, 'publish', paused):
            data = self.run_product()
        self.assertEqual(data['state'], 'paused', data)
        self.assertEqual(self.journal.receipts(), [])
        with patch.object(Verifier, 'run', side_effect=AssertionError('Saved checks remain current')):
            data = self.resume()
        self.assertEqual(data['state'], 'milestone_closed', data)

    def test_subjective_criterion_needs_explicit_candidate_bound_human_acceptance(self):
        self.product(inter=False, custom=lambda p: p['milestones'][0].update(subjective_criteria=[1]))
        self.sdk.responses = [implementation_a(), implementation_b()]
        def configure(p, d):
            check = dict(next(c for c in d['checks'] if c['id'] == 'integrated'))
            check.update(id='human', kind='human_review', target='Review clarity of the composed result', criteria=[1], cases=[])
            d['checks'].append(check)
        self.configure(configure)
        data = self.run_product()
        self.assertEqual(data['state'], 'waiting_decision', data)
        self.assertEqual(self.journal.receipts(), [])
        decision = self.store.snapshot()['decisions'][-1]
        self.assertIn(self.git('rev-parse', 'factory/accepted'), decision['question'])
        self.service.answer_decision(decision['id'], 'accept')
        self.service.run_pending(*self.jobs[-1])
        data = self.groups.inspect()
        self.assertEqual(data['state'], 'milestone_closed', data)
        evidence = self.journal.receipts()[0]['evidence']
        self.assertEqual(next(e for e in evidence if e['check_id'] == 'human')['decision']['answer'], 'accept')
        self.assertEqual(len(self.sdk.contexts), 2)

    def test_subjective_mapping_cannot_be_replaced_by_mechanical_pass(self):
        self.product(custom=lambda p: p['milestones'][0].update(subjective_criteria=[1]))
        data = self.run_product()
        self.assertEqual(data['state'], 'validation_pending', data)
        self.assertIn('subjective_criteria_require_human_review', data['diagnostic']['pending'])

    def test_explicit_requirement_acceptance_requires_all_contributors_and_current_oracle(self):
        self.product()
        key = self.store.snapshot()['planning']['roadmap']['plan']['coverage'][0]['requirement']
        self.configure(lambda p, d: d.update(requirement_acceptance=[{'requirement': key,
            'condition': 'The approved signed composition and description checks pass on the integrated result',
            'milestones': ['m1', 'm2'], 'checks': ['description_close']}]))
        self.assertEqual(self.run_product()['state'], 'project_ready_for_validation')
        requirement = next(r for r in self.service.inspect(view='requirements')['requirements'] if r['key'] == key)
        self.assertEqual(requirement['progress']['status'], 'satisfied')
        self.assertEqual(requirement['progress']['acceptance']['commit'], self.journal.receipts()[-1]['commit'])

    def test_preparation_has_one_correction_with_persistent_budget(self):
        self.product()
        self.refiner.responses = [{}, {}]
        data = self.run_product()
        self.assertEqual(data['diagnostic']['code'], 'refinement_exhausted', data)
        self.assertEqual(data['budget']['preparation_calls'], 2)
        self.assertEqual(len(self.journal.receipts()), 1)
        data = self.resume()
        self.assertEqual(data['budget']['preparation_calls'], 2)
        self.assertEqual(len(self.refiner.contexts), 2)

    def test_old_process_cannot_publish_closure(self):
        self.product(inter=False)
        original = MilestoneGate.publish
        def superseded(gate, validation, checks):
            Runtime(self.store).queue()
            return original(gate, validation, checks)
        with patch.object(MilestoneGate, 'publish', superseded), self.assertRaises(FactoryError):
            self.run_product()
        self.assertEqual(self.journal.receipts(), [])

    def test_same_closure_problem_cannot_start_another_cycle_on_another_commit(self):
        self.product(inter=False)
        self.assertEqual(self.run_product()['state'], 'milestone_closed')
        issue = self.journal.issues()[0]
        original = self.journal.rows('milestone_validations')[0]
        repeated = dict(original, id='second-observation')
        failure = dict(issue['history'][0]['evidence'], commit=self.git('rev-parse', 'factory/accepted'))
        failure['log'] += '\nA different failure message on a later commit'
        group = self.groups.latest()
        gate = MilestoneGate(Continuation(self.service, self.store), group, self.store.snapshot(),
            self.executions.definition(group['policy']['definition_id'])['verification'])
        with self.assertRaises(FactoryError) as error:
            gate.handle_failures(repeated, [failure])
        self.assertEqual(error.exception.code, 'remediation_exhausted')
        self.assertEqual(len(self.sdk.contexts), 3)
        self.assertEqual(self.journal.issues()[0]['attempts'], 1)

    def test_absent_test_file_is_not_run_and_not_sent_to_implementation(self):
        self.product()
        self.configure(lambda p, d: next(c for c in d['checks'] if c['id'] == 'integrated').update(target='absent.py'))
        data = self.run_product()
        self.assertEqual(data['state'], 'validation_pending', data)
        self.assertEqual(self.journal.issues()[0]['classification'], 'infrastructure')
        self.assertEqual(self.journal.rows('milestone_validations')[0]['evidence'][0]['status'], 'NOT_RUN')
        self.assertEqual(len(self.sdk.contexts), 2)

    def test_supported_system_checkpoint_executes_on_integrated_candidate(self):
        def custom(plan):
            gate = dict(next(g for g in plan['gates'] if g['id'] == 'milestone_gate'))
            gate.update(id='system', kind='system', trigger='project_checkpoint', checks=['Verify signed sum contract'], harness=[])
            plan['gates'].append(gate)
        self.product(inter=False, custom=custom)
        self.sdk.responses = [implementation_a(), implementation_b()]
        def configure(p, d):
            check = dict(d['checks'][0]); check.update(id='system_check', gate='system', criteria=[],
                cases=[{'args_json': '[-1,2]', 'expected_json': '1'}])
            d['checks'].append(check)
        self.configure(configure)
        self.assertEqual(self.run_product()['state'], 'milestone_closed')
        evidence = self.journal.receipts()[0]['evidence']
        self.assertEqual(next(e for e in evidence if e['check_id'] == 'system_check')['trigger'], 'project_checkpoint')

    def test_closure_receipt_is_historical_after_verification_definition_change(self):
        self.product(inter=False)
        self.assertEqual(self.run_product()['state'], 'milestone_closed')
        old = self.journal.receipts()[0]
        self.configure(lambda p, d: next(c for c in d['checks'] if c['id'] == 'integrated').update(timeout_seconds=6))
        self.assertEqual(self.journal.receipts(), [old])
        self.assertFalse(self.service.inspect(view='milestones')['receipts'][0]['current_sources'])
        self.assertEqual(self.journal.closed(self.groups.latest()['sources']), {})

    def test_deadline_prevents_gate_publication_even_with_all_checks_passed(self):
        self.product(inter=False)
        self.sdk.responses = [implementation_a(), implementation_b()]
        original = MilestoneGate.publish
        def expire(gate, validation, checks):
            gate.group['deadline'] = 0
            gate.controller.journal.save(gate.group)
            return original(gate, validation, checks)
        with patch.object(MilestoneGate, 'publish', expire):
            data = self.run_product()
        self.assertEqual(data['state'], 'budget_exhausted', data)
        self.assertEqual(self.journal.receipts(), [])

    def test_step9_terminal_run_migrates_without_resetting_budget_or_expanding_authorization(self):
        self.product(inter=False)
        self.service.execute_next_slice(request_id='legacy')
        group = self.groups.latest()
        group.update(state='milestone_ready')
        with self.store._connection(write=True) as db:
            db.execute("UPDATE continuations SET state='milestone_ready',data=?", (json.dumps(group),))
            for table in ('milestone_reviews', 'milestone_issues', 'milestone_acceptances', 'milestone_validations'):
                db.execute('DROP TABLE ' + table)
            db.execute('PRAGMA user_version=7')
        self.store.initialize()
        migrated = self.groups.latest()
        self.assertEqual(migrated['state'], 'checkpoint')
        self.assertEqual(migrated['legacy_stop'], 'milestone_ready')
        self.assertEqual((migrated['deadline'], migrated['limits']), (group['deadline'], group['limits']))
        self.assertFalse(migrated['limits']['inter_milestone'])
        self.assertEqual(self.journal.receipts(), [])


if __name__ == '__main__':
    unittest.main()
