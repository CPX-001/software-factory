from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from factory.application import FactoryService
from factory.discovery import Discovery
from factory.mcp_server import FactoryTools
from factory.planning import (Planning, append_revision, classify, executable_slices, export_roadmap,
                              quality_gate, refinement_snapshot, source_snapshot, MAX_CALLS)
from factory.registry import Registry
from factory.runtime import Runtime
from factory.skill_catalog import Catalog
from factory.skill_router import SkillRouter
from factory.workflow import Store, WorkflowError
from tests.discovery_fakes import FakeModel, complete_reply
from tests.architecture_fakes import FakeArchitect, finish as finish_architecture, question, review
from tests.planning_fakes import proposal, dynamic, fake


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root); self.store.initialize()
        Discovery(self.store, FakeModel(complete_reply())).submit('Complete brief')
        finish_architecture(self.store)
        self.source = source_snapshot(self.store.snapshot())
        self.plan = proposal(self.source)
        self.router = SkillRouter(Catalog())

    def run_plan(self, *responses, stop=None):
        model = FakeArchitect(*(responses or (self.plan, review())))
        self.model = model
        return Planning(self.store, model, self.router, should_stop=stop).run()

    def gate(self, plan=None, **kwargs):
        return quality_gate(plan or self.plan, self.source, **kwargs)

    def test_roadmap_milestones_slices_harness_and_projection_are_persisted_atomically(self):
        baseline = deepcopy(self.store.snapshot()['architecture']['baseline'])
        snapshot = self.run_plan()
        self.assertEqual(snapshot['phase'], 'execution')
        state = snapshot['planning']
        self.assertEqual(state['stage'], 'completed')
        self.assertTrue(state['roadmap']['gate']['passed'])
        self.assertEqual(state['roadmap']['plan'], self.plan)
        self.assertEqual(state['roadmap']['architecture_fingerprint'], baseline['fingerprint'])
        self.assertEqual(snapshot['architecture']['baseline'], baseline)
        path = self.root / '.factory/planning/ROADMAP.md'
        self.assertEqual(path.read_text(), state['roadmap']['projection'])
        self.assertIn('milestone_gate', path.read_text())
        self.assertEqual(self.store.events()[-1]['kind'], 'planning_completed')
        self.assertEqual([p.name for p in self.root.iterdir()], ['.factory'])

    def test_completed_planning_is_idempotent_and_markdown_is_repairable(self):
        self.run_plan()
        before = self.store.snapshot()
        path = self.root / '.factory/planning/ROADMAP.md'
        path.write_text('not authoritative')
        Planning(Store(self.root), FakeArchitect(), self.router).run()
        self.assertEqual(before, self.store.snapshot())
        self.assertEqual(path.read_text(), before['planning']['roadmap']['projection'])

    def test_projection_failure_can_be_recovered_without_repeating_paid_calls(self):
        with patch('factory.planning.export_roadmap', side_effect=OSError('Disk full')):
            with self.assertRaises(OSError):
                self.run_plan()
        self.assertEqual(self.store.snapshot()['phase'], 'execution')
        export_roadmap(self.store)
        self.assertTrue((self.root / '.factory/planning/ROADMAP.md').exists())

    def test_progressive_plan_keeps_future_slice_outline(self):
        future = deepcopy(self.plan['slices'][0])
        future.update(id='s2', title='Advanced variants', maturity='outline', dependencies=['s1'],
                      requirements=[], scope=[], acceptance_criteria=[], verification_expectation='')
        future['kind'] = 'vertical'
        # A future vertical slice still has traceable requirements, even without detailed acceptance.
        key = self.plan['coverage'][-1]['requirement']
        future['requirements'] = [key]
        self.plan['coverage'][-1]['slices'].append('s2')
        self.plan['slices'].append(future)
        self.assertTrue(self.gate()['passed'], self.gate())
        self.run_plan()
        self.assertEqual(self.store.snapshot()['planning']['roadmap']['plan']['slices'][1]['maturity'], 'outline')
        self.assertEqual([s['id'] for s in executable_slices(self.plan)], ['s1'])

    def test_future_execution_ready_outside_horizon_rejected(self):
        self.plan['near_term'] = []
        self.assertIn('detail_outside_horizon:s1', self.gate()['errors'])
        self.assertIn('no_initial_executable_slice', self.gate()['errors'])

    def test_all_active_requirements_have_bidirectional_owners(self):
        key = self.plan['coverage'].pop()['requirement']
        self.assertIn('unowned_requirement:' + key, self.gate()['errors'])
        self.assertIn('reverse_milestone_coverage:' + key, self.gate()['errors'])
        self.plan['coverage'].append({'requirement': key, 'disposition': 'deferred', 'milestone': 'm1',
                                     'slices': [], 'rationale': 'Explicitly postponed until scope expands'})
        self.plan['slices'][0]['requirements'].remove(key)
        self.assertTrue(self.gate()['passed'], self.gate())
        self.plan['coverage'][-1]['disposition'] = 'out_of_scope'
        self.assertTrue(self.gate()['passed'])
        self.plan['coverage'][-1]['disposition'] = 'blocked'
        self.assertIn('blocked_requirement:' + key, self.gate()['errors'])

    def test_architecture_binding_on_every_element(self):
        for target in (self.plan, self.plan['milestones'][0], self.plan['slices'][0]):
            old = deepcopy(target['architecture'])
            target['architecture']['fingerprint'] = 'sha256:invalid'
            self.assertTrue(any(e.startswith('architecture_binding') for e in self.gate()['errors']))
            target['architecture'] = old
        self.plan['slices'][0].update(architecture_impact='potential_change', impact_reason='May need a new external boundary')
        before = self.store.snapshot()['architecture']
        self.run_plan()
        self.assertEqual(self.store.snapshot()['architecture'], before)

    def test_cycles_unknown_references_and_missing_success_criteria(self):
        self.plan['slices'][0]['dependencies'] = ['s1']
        self.assertIn('dependency_cycle', self.gate()['errors'])
        self.plan['slices'][0]['dependencies'] = ['missing']
        self.assertIn('slice_dependencies:s1', self.gate()['errors'])
        self.plan['milestones'][0]['dependencies'] = ['m1']
        self.assertIn('dependency_cycle', self.gate()['errors'])
        self.plan['milestones'][0]['success_criteria'] = []
        self.assertIn('milestone_criteria:m1', self.gate()['errors'])

    def test_cross_milestone_cycle_is_detected(self):
        m2 = deepcopy(self.plan['milestones'][0]); m2.update(id='m2', dependencies=['m1'])
        s2 = deepcopy(self.plan['slices'][0]); s2.update(id='s2', milestone='m2')
        self.plan['milestones'].append(m2); self.plan['slices'].append(s2)
        self.plan['slices'][0]['dependencies'] = ['s2']
        self.assertIn('dependency_cycle', self.gate()['errors'])

    def test_quality_gate_cannot_be_bypassed_by_transition(self):
        with self.assertRaisesRegex(WorkflowError, 'quality gate'):
            self.store.transition('execution', self.store.snapshot()['revision'])
        self.plan['blockers'] = ['Unresolved product boundary']
        snapshot = self.run_plan(self.plan, review(), self.plan, review())
        self.assertEqual(snapshot['phase'], 'planning')
        self.assertEqual(snapshot['planning']['stage'], 'blocked')
        self.assertIsNone(snapshot['planning']['roadmap'])
        self.assertEqual(len(self.model.contexts), 4)
        Planning(self.store, FakeArchitect(), self.router).run()

    def test_review_is_bounded_and_disagreement_is_a_durable_blocker(self):
        finding = {'id': 'large', 'severity': 'high', 'category': 'slice_size', 'targets': ['s1'],
                   'description': 'Slice exceeds a coherent user outcome', 'recommendation': 'Split vertical outcomes'}
        snapshot = self.run_plan(self.plan, review([finding]), self.plan, review([finding]))
        self.assertEqual(snapshot['planning']['stage'], 'blocked')
        self.assertEqual(snapshot['planning']['critic_calls'], 2)
        self.assertEqual(snapshot['planning']['reconciliations'], 1)
        self.assertEqual([c['role'] for c in self.model.contexts], ['propose', 'critic', 'reconcile', 'critic_final'])
        self.assertEqual(Planning(Store(self.root), FakeArchitect(), self.router).run(), snapshot)

    def test_trivial_classification_omits_review(self):
        # Conservative classification permits only an explicitly small local offline product.
        for r in self.source['requirements']:
            r['text'] = 'none' if r['category'] == 'integrations' else 'offline personal single user utility'
        self.assertFalse(classify(self.source, self.plan)['required'])
        with patch('factory.planning.source_snapshot', return_value=self.source):
            self.run_plan(self.plan)
        self.assertEqual(len(self.model.contexts), 1)
        self.assertFalse(self.store.snapshot()['planning']['roadmap']['review']['classification']['required'])

    def test_human_answer_resumes_planning_without_restarting_architecture(self):
        p = deepcopy(self.plan); p['unresolved_questions'] = [question()]
        first = self.run_plan(p)
        self.assertEqual(first['phase'], 'planning')
        decision = first['planning']['decision_details'][0]
        self.assertIsNone(first['planning']['roadmap'])
        self.store.answer_decision(decision['id'], 'Shared database', first['revision'])
        final = deepcopy(self.plan); final['decision_keys'] = [decision['key']]
        before = self.store.snapshot()['architecture']
        self.run_plan(final, review())
        self.assertEqual(self.model.contexts[0]['human_answers'][-1]['answer'], 'Shared database')
        self.assertEqual(self.store.snapshot()['phase'], 'execution')
        self.assertEqual(self.store.snapshot()['architecture'], before)

    def test_pending_decisions_and_missing_incorporation_block_gate(self):
        d = {'key': 'scope', 'answer': None, 'origin': 'planning'}
        self.assertIn('pending_human_decisions', self.gate(decisions=[d])['errors'])
        d['answer'] = 'Keep local'
        self.assertIn('human_answer_not_incorporated:scope', self.gate(decisions=[d])['errors'])

    def test_pause_checkpoint_and_restart_does_not_repeat_saved_proposal(self):
        paused = False
        def respond(_):
            nonlocal paused
            paused = True
            return self.plan
        snapshot = self.run_plan(respond, stop=lambda: paused)
        self.assertEqual(snapshot['planning']['stage'], 'critic')
        self.assertEqual(snapshot['phase'], 'planning')
        model = FakeArchitect(review())
        Planning(Store(self.root), model, self.router).run()
        self.assertEqual([c['role'] for c in model.contexts], ['critic'])
        self.assertEqual(self.store.snapshot()['phase'], 'execution')

    def test_invalid_response_stale_output_and_failed_attempts_are_persistent(self):
        for _ in range(MAX_CALLS):
            with self.assertRaises(WorkflowError):
                self.run_plan({})
        snapshot = Planning(Store(self.root), FakeArchitect(), self.router).run()
        self.assertEqual(snapshot['planning']['stage'], 'blocked')
        self.assertEqual(snapshot['planning']['calls'], MAX_CALLS)
        with self.store._connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM planning_calls WHERE status='failed'").fetchone()[0], MAX_CALLS)

    def test_concurrent_decision_prevents_stale_response_commit(self):
        def concurrent(_):
            self.store.request_decision('Material scope change?', self.store.snapshot()['revision'])
            return self.plan
        with self.assertRaisesRegex(WorkflowError, 'Stale revision'):
            self.run_plan(concurrent)
        self.assertIsNone(self.store.snapshot()['planning']['proposal'])
        self.assertEqual(self.store.snapshot()['status'], 'waiting_for_human')

    def test_interrupted_call_is_counted_on_restart(self):
        def interruption(_):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.run_plan(interruption)
        with self.store._connection(write=True) as db:
            db.execute("UPDATE planning_calls SET status='pending'")
        self.run_plan(self.plan, review())
        with self.store._connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM planning_calls WHERE status='interrupted'").fetchone()[0], 1)
        self.assertEqual(self.store.snapshot()['planning']['calls'], 3)

    def test_transaction_failure_does_not_publish_partial_roadmap(self):
        with self.store._connection(write=True) as db:
            db.execute("CREATE TRIGGER reject_completion BEFORE INSERT ON events WHEN NEW.kind='planning_completed' BEGIN SELECT RAISE(ABORT, 'fail'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.run_plan()
        self.assertEqual(self.store.snapshot()['phase'], 'planning')
        self.assertIsNone(self.store.snapshot()['planning']['roadmap'])
        self.assertEqual(self.store.snapshot()['planning']['stage'], 'gate')

    def test_verification_is_local_by_default_and_harness_is_just_a_requirement(self):
        self.assertTrue(self.gate()['passed'])
        self.assertFalse(any(g['kind'] in ('integration', 'system') for g in self.plan['gates']))
        self.plan['gates'][0]['cost'] = 'expensive'
        self.assertIn('local_gate_cost_or_trigger:local_gate', self.gate()['errors'])

    def test_boundary_persistence_security_public_api_require_strategic_gate(self):
        for signal in ('cross_component', 'boundary', 'persistence', 'security', 'public_api'):
            self.plan['slices'][0]['verification_triggers'] = [signal]
            self.assertIn('integration_gate_missing:s1:' + signal, self.gate()['errors'])
        self.plan['gates'].append({'id': 'security_gate', 'kind': 'integration', 'trigger': 'after_slice',
            'target': 's1', 'checks': ['API contract validation'], 'harness': [], 'cost': 'moderate',
            'rationale': 'Public API compatibility', 'signals': ['public_api']})
        self.assertTrue(self.gate()['passed'], self.gate())
        self.plan['gates'].append({'id': 'release_gate', 'kind': 'system', 'trigger': 'project_checkpoint',
            'target': 'm1', 'checks': ['Validate complete user journey at release'], 'harness': [], 'cost': 'expensive',
            'rationale': 'Release readiness', 'signals': ['release']})
        self.assertTrue(self.gate()['passed'], self.gate())

    def test_harness_introduction_cannot_follow_consumer(self):
        h = self.plan['harness'][0]
        h['needed_by_gates'] = ['local_gate']
        self.assertIn('harness_gate_links:unit', self.gate()['errors'])
        h['needed_by_gates'] = ['local_gate', 'milestone_gate']
        local = deepcopy(self.plan['gates'][0]); local.update(id='focused', harness=[])
        self.plan['gates'].append(local)
        self.plan['slices'][0]['verification_triggers'] = ['persistence']
        self.plan['gates'][0].update(kind='integration', trigger='before_slice', signals=['persistence'])
        self.assertIn('harness_too_late:unit:local_gate', self.gate()['errors'])
        h['when'] = 'before_slice'
        self.assertTrue(self.gate()['passed'], self.gate())
        h['introduced_by'] = 'missing'
        self.assertIn('harness_introduction:unit', self.gate()['errors'])

    def test_critical_risk_requires_owner_mitigation_and_early_validation(self):
        risk = {'id': 'external_capability', 'severity': 'critical', 'description': 'External API may not support core capability',
            'owner': 's1', 'mitigation': 'Validate the contract before building dependent features',
            'acceptance_key': '', 'validation_slice': 's1', 'blocks': []}
        self.plan['risks'] = [risk]; self.plan['slices'][0]['risks'] = [risk['id']]
        self.plan['slices'][0]['kind'] = 'risk_probe'
        self.assertTrue(self.gate()['passed'], self.gate())
        risk['owner'] = 'missing'
        self.assertIn('risk_owner:external_capability', self.gate()['errors'])
        risk['owner'] = 's1'; risk['mitigation'] = ''
        self.assertIn('unmitigated_risk:external_capability', self.gate()['errors'])
        risk.update(acceptance_key='accept_risk', validation_slice='')
        self.plan['slices'][0]['kind'] = 'vertical'
        self.assertTrue(self.gate(decisions=[{'key': 'accept_risk', 'answer': 'accept'}])['passed'])

    def test_baseline_risk_cannot_disappear_or_be_downgraded(self):
        self.source['baseline']['risks'] = [{'id': 'api_risk', 'severity': 'critical'}]
        self.assertIn('baseline_risk_missing:api_risk', self.gate()['errors'])
        self.plan['risks'] = [{'id': 'api_risk', 'severity': 'low', 'description': 'Risk', 'owner': 's1',
            'mitigation': '', 'acceptance_key': '', 'validation_slice': '', 'blocks': []}]
        self.assertIn('risk_downgraded:api_risk', self.gate()['errors'])

    def test_compact_context_no_transcript_history_catalog_or_unrelated_refinement(self):
        self.run_plan()
        context = self.model.contexts[0]
        self.assertEqual(set(context['source']), {'requirements', 'architecture', 'baseline', 'adrs', 'decisions'})
        self.assertNotIn('turns', json.dumps(context))
        self.assertNotIn('events', context)
        self.assertNotIn('catalog', context['skills'])
        small = refinement_snapshot(self.store.snapshot(), 's1', [{'slice': 'earlier', 'result': 'observed outcome'}])
        self.assertEqual(small['slice']['id'], 's1')
        self.assertEqual(small['architecture'], self.source['architecture'])
        self.assertEqual(small['evidence'][0]['result'], 'observed outcome')
        self.assertNotIn('roadmap', small)
        with self.assertRaises(WorkflowError):
            refinement_snapshot(self.store.snapshot(), 'missing')

    def test_revision_history_and_completed_work_are_immutable(self):
        self.run_plan()
        original = self.store.snapshot()['planning']['roadmap']
        revised = deepcopy(self.plan)
        revised['slices'][0]['title'] = 'More precise upcoming outcome'
        with self.store._connection(write=True) as db:
            append_revision(db, revised, self.source, {}, {'passed': True, 'errors': []}, 'Refine upcoming slice', expected_parent=1)
        self.assertEqual(self.store.snapshot()['planning']['roadmap']['revision'], 2)
        with self.store._connection() as db:
            self.assertEqual(json.loads(db.execute('SELECT plan FROM planning_revisions WHERE revision=1').fetchone()[0]), original['plan'])
        # Simulate future evidence-backed completion through the persistence primitive.
        revised['slices'][0]['maturity'] = 'completed'
        with self.store._connection(write=True) as db:
            append_revision(db, revised, self.source, {}, {}, 'Future completion checkpoint', expected_parent=2)
        changed = deepcopy(revised); changed['slices'][0]['title'] = 'Rewrite history'
        with self.assertRaisesRegex(WorkflowError, 'immutable'):
            with self.store._connection(write=True) as db:
                append_revision(db, changed, self.source, {}, {}, 'Invalid rewrite', expected_parent=3)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store._connection(write=True) as db:
                db.execute("UPDATE planning_revisions SET reason='rewrite' WHERE revision=1")
        with self.assertRaisesRegex(WorkflowError, 'Stale planning'):
            with self.store._connection(write=True) as db:
                append_revision(db, self.plan, self.source, {}, {}, 'Stale update', expected_parent=1)

    def test_v4_migration_preserves_architecture_and_workflow(self):
        before = self.store.snapshot()
        with sqlite3.connect(self.store.path) as db:
            for table in ('planning_current', 'planning_revisions', 'planning_decisions', 'planning_calls', 'planning_run'):
                db.execute('DROP TABLE ' + table)
            db.execute('PRAGMA user_version=4')
        self.assertEqual(Store(self.root).snapshot()['planning']['stage'], 'not_started')
        Store(self.root).initialize()
        self.assertEqual(before, self.store.snapshot())

    def test_future_slice_order_dependency_and_open_milestone_can_change_after_completion(self):
        future = deepcopy(self.plan['slices'][0])
        future.update(id='s2', milestone='m2', maturity='outline', dependencies=['s1'], scope=[], acceptance_criteria=[])
        key = self.plan['coverage'][-1]['requirement']
        future['requirements'] = [key]
        m2 = deepcopy(self.plan['milestones'][0])
        m2.update(id='m2', requirements=[key], dependencies=['m1'], verification_gates=['gate2'])
        self.plan['milestones'][0]['requirements'].remove(key)
        self.plan['slices'][0]['requirements'].remove(key)
        self.plan['coverage'][-1].update(milestone='m2', slices=['s2'])
        self.plan['milestones'].append(m2); self.plan['slices'].append(future)
        self.plan['gates'].append(dict(deepcopy(self.plan['gates'][1]), id='gate2', target='m2', harness=[]))
        self.run_plan()
        completed = deepcopy(self.plan)
        completed['slices'][0]['maturity'] = 'completed'
        completed['milestones'][0]['status'] = 'completed'
        with self.store._connection(write=True) as db:
            append_revision(db, completed, self.source, {}, {}, 'Future completion evidence checkpoint', expected_parent=1)
        revised = deepcopy(completed)
        revised['slices'][1].update(title='Refined future outcome', dependencies=[], scope=['New justified scope'])
        revised['slices'].reverse()
        revised['milestones'][1]['objective'] = 'Updated future capability after actual evidence'
        with self.store._connection(write=True) as db:
            append_revision(db, revised, self.source, {}, {}, 'Refine future work using actual evidence', expected_parent=2)
        persisted = self.store.snapshot()['planning']['roadmap']['plan']
        self.assertEqual(persisted['slices'][1], completed['slices'][0])
        self.assertEqual(persisted['milestones'][0], completed['milestones'][0])
        self.assertEqual(persisted['slices'][0]['id'], 's2')
        changed = deepcopy(revised); changed['slices'][0]['milestone'] = 'm1'
        with self.assertRaisesRegex(WorkflowError, 'membership is immutable'):
            with self.store._connection(write=True) as db:
                append_revision(db, changed, self.source, {}, {}, 'Cannot add to completed milestone', expected_parent=3)

    def test_near_term_cannot_hide_unresolved_milestone_dependencies(self):
        future = deepcopy(self.plan['slices'][0]); future.update(id='s2', milestone='m2')
        m2 = deepcopy(self.plan['milestones'][0]); m2.update(id='m2', dependencies=['m1'])
        self.plan['milestones'].append(m2); self.plan['slices'].append(future)
        self.plan['slices'][0]['maturity'] = 'outline'
        self.plan['near_term'] = ['s2']
        self.assertIn('unrefined_dependency:s2', self.gate()['errors'])
        self.assertIn('no_initial_executable_slice', self.gate()['errors'])

    def test_risk_dependents_must_follow_validation(self):
        risk = {'id': 'api_probe', 'severity': 'critical', 'description': 'API capability unknown',
            'owner': 's1', 'mitigation': 'Verify with representative response', 'acceptance_key': '',
            'validation_slice': 's1', 'blocks': ['s2']}
        self.plan['risks'] = [risk]; self.plan['slices'][0]['risks'] = ['api_probe']
        future = deepcopy(self.plan['slices'][0]); future.update(id='s2', dependencies=[], maturity='outline')
        self.plan['slices'].append(future)
        for c in self.plan['coverage']:
            c['slices'].append('s2')
        self.assertIn('risk_order:api_probe:s2', self.gate()['errors'])
        future['dependencies'] = ['s1']
        self.assertTrue(self.gate()['passed'], self.gate())
        self.plan['slices'][0]['maturity'] = 'outline'
        self.assertIn('critical_risk_not_early:api_probe', self.gate()['errors'])

    def test_missing_required_skill_blocks_before_any_paid_call(self):
        from factory.skill_router import Policy
        router = SkillRouter(Catalog(), Policy(required=('missing-policy',)))
        model = FakeArchitect(self.plan)
        with self.assertRaises(WorkflowError):
            Planning(self.store, model, router).run()
        self.assertFalse(model.contexts)
        self.assertEqual(self.store.snapshot()['planning']['calls'], 0)

    def test_changed_source_is_blocked_without_architecture_reevaluation(self):
        self.run_plan(self.plan, stop=lambda: bool(self.store.snapshot()['planning']['proposal']))
        changed = deepcopy(self.source); changed['requirements'][0]['text'] = 'Changed scope'
        with patch('factory.planning.source_snapshot', return_value=changed):
            snapshot = Planning(self.store, FakeArchitect(), self.router).run()
        self.assertEqual(snapshot['planning']['stage'], 'blocked')
        self.assertEqual(snapshot['architecture']['baseline']['revision'], 1)


class PlanningServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.registry = Registry(self.root / 'registry'); self.registry.allow_root(self.root)
        self.jobs = []
        from tests.architecture_fakes import proposal as architecture_proposal
        self.service = FactoryService(self.registry, discovery_model=FakeModel(complete_reply()),
            architecture_model=FakeArchitect(architecture_proposal(), review()), planning_model=fake(),
            router=SkillRouter(Catalog()), launcher=lambda p, r: self.jobs.append((p, r)))
        self.service.initialize_project(str(self.root / 'project'))
        self.tools = FactoryTools(self.service)

    def drive(self):
        self.service.run_pending(*self.jobs[-1])

    def test_autonomous_discovery_architecture_planning_stop_and_mcp_inspection(self):
        self.service.submit_user_message('Build the product'); self.drive()
        status = self.service.get_status()
        self.assertEqual(status['phase'], 'execution')
        self.assertEqual(status['autonomous_run']['status'], 'implementation_boundary')
        self.assertTrue(status['capabilities']['planning_implemented'])
        for view in ('plan', 'milestones', 'next_slice', 'requirements', 'verification'):
            result = self.tools.call('factory_inspect', {'view': view})
            self.assertTrue(result['ok'], result)
            self.assertTrue(result['data']['accepted'])
        self.assertEqual(self.tools.call('factory_inspect', {'view': 'next_slice'})['data']['slice']['id'], 's1')
        refinement = self.tools.call('factory_inspect', {'view': 'refinement', 'slice_id': 's1'})
        self.assertTrue(refinement['ok'], refinement)
        self.assertFalse(self.tools.call('factory_inspect', {'view': 'plan', 'slice_id': 's1'})['ok'])
        self.service.resume()
        self.assertEqual(len(self.jobs), 1)
        self.assertEqual(list((self.root / 'project').iterdir()), [self.root / 'project/.factory'])

    def test_planning_human_decision_answer_automatically_schedules_continuation(self):
        def ask(context):
            p = dynamic(context); p['unresolved_questions'] = [question()]
            return p
        self.service.planning_model = FakeArchitect(ask, dynamic, review())
        self.service.submit_user_message('Build'); self.drive()
        self.assertEqual(self.service.get_status()['phase'], 'planning')
        self.assertEqual(self.service.get_status()['autonomous_run']['status'], 'waiting_for_human')
        d = self.tools.call('factory_decisions', {})['data']['items'][0]
        self.assertEqual(d['options'], question()['options'])
        answer = self.tools.call('factory_answer', {'decision_id': d['id'], 'answer': 'Shared database'})
        self.assertTrue(answer['ok'], answer)
        self.assertEqual(len(self.jobs), 2)
        self.drive()
        self.assertEqual(self.service.get_status()['phase'], 'execution')

    def test_pause_during_planning_survives_answer_and_restart(self):
        def ask(context):
            self.service.pause()
            p = dynamic(context); p['unresolved_questions'] = [question()]
            return p
        self.service.planning_model = FakeArchitect(ask, dynamic, review())
        self.service.submit_user_message('Build'); self.drive()
        d = self.service.list_pending_decisions()['items'][0]
        self.service.answer_decision(d['id'], 'Shared database')
        self.assertEqual(len(self.jobs), 1)
        self.assertEqual(FactoryService(self.registry).get_status()['state'], 'paused')
        self.service.resume(); self.drive()
        self.assertEqual(self.service.get_status()['phase'], 'execution')


class PlanningAdapterTests(unittest.TestCase):
    def test_native_skills_fresh_review_readonly_and_no_api_key_fallback(self):
        import sys
        from factory.codex_planning import CodexPlanning
        from factory.planning_contract import PLAN_SCHEMA, REVIEW_SCHEMA
        from tests.test_architecture import ArchitectureAdapterTests
        sdk, codex = ArchitectureAdapterTests().sdk()
        with patch.dict(sys.modules, {'openai_codex': sdk}):
            adapter = CodexPlanning()
            adapter.respond({'role': 'propose'}, skill_inputs=[{'name': 'policy', 'path': '/skills/SKILL.md'}])
            self.assertEqual(codex.thread_start.return_value.run.call_args.kwargs['output_schema'], PLAN_SCHEMA)
            self.assertEqual(codex.thread_start.return_value.run.call_args.args[0][1].kind, 'skill')
            adapter.respond({'role': 'critic'}, skill_inputs=[])
            self.assertEqual(codex.thread_start.return_value.run.call_args.kwargs['output_schema'], REVIEW_SCHEMA)
            self.assertEqual(codex.thread_start.call_count, 2)
            self.assertTrue(codex.thread_start.call_args.kwargs['ephemeral'])
            self.assertEqual(codex.thread_start.call_args.kwargs['sandbox'], 'read-only')
            self.assertEqual(sdk.Codex.call_args.kwargs['config'].env['OPENAI_API_KEY'], '')
            codex.account.return_value.account.root.type = 'apiKey'
            with self.assertRaisesRegex(WorkflowError, 'API-key'):
                adapter.respond({'role': 'propose'}, skill_inputs=[])

    def test_planning_routes_real_candidate_names_and_specification_without_catalog_context(self):
        from factory.skill_catalog import Skill
        from factory.skill_router import Policy, Work
        names = ('planning-and-task-breakdown', 'spec-driven-development', 'unrelated', 'required-policy')
        router = SkillRouter(Catalog(tuple(Skill(n, n, '/skills/' + n + '/SKILL.md') for n in names)),
                             Policy(required=('required-policy',)))
        route = router.route(Work(domain='planning', intent='plan', concerns=('specification',)))
        self.assertEqual(route.recommended, ['planning-and-task-breakdown', 'spec-driven-development'])
        self.assertEqual(route.required_inputs(router.catalog)[0]['name'], 'required-policy')
        self.assertNotIn('unrelated', route.context())


if __name__ == '__main__':
    unittest.main()
