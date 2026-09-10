from copy import deepcopy
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from factory.architecture import Architecture, MAX_CALLS, classify, fingerprint, quality_gate, source_snapshot
from factory.architecture_contract import ARCHITECTURE_SCHEMA, REVIEW_SCHEMA
from factory.codex_architecture import CodexArchitecture
from factory.discovery import Discovery
from factory.skill_catalog import Catalog, Skill
from factory.skill_router import Policy, SkillRouter
from factory.workflow import Store, WorkflowError, next_action
from tests.architecture_fakes import FakeArchitect, finding, proposal, question, review
from tests.discovery_fakes import FakeModel, complete_reply


class ArchitectureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)
        self.store = Store(self.project)
        self.store.initialize()
        Discovery(self.store, FakeModel(complete_reply())).submit('SECRET TRANSCRIPT NOT ARCHITECTURE CONTEXT')
        self.router = SkillRouter(Catalog())

    def run_arch(self, *responses):
        model = FakeArchitect(*responses)
        return Architecture(self.store, model, self.router).run(), model

    def test_baseline_persistence_revision_adrs_projection_and_transition(self):
        result, model = self.run_arch(proposal(), review())
        baseline = result['architecture']['baseline']
        self.assertEqual(result['phase'], 'planning')
        self.assertEqual(len(model.contexts), 2)
        self.assertEqual([c['role'] for c in model.contexts], ['propose', 'critic'])
        self.assertNotIn('SECRET TRANSCRIPT', json.dumps(model.contexts))
        self.assertEqual(baseline['revision'], 1)
        self.assertEqual(baseline['fingerprint'], fingerprint(proposal()))
        self.assertIn('Architecture revision 1', baseline['projection'])
        self.assertEqual(len(result['architecture']['adrs']), 1)
        self.assertTrue(baseline['gate']['passed'])
        self.assertEqual(Store(self.project).snapshot(), result)
        self.assertEqual(next_action(result), {'action': 'planning', 'implemented': True, 'stage': 'not_started', 'blockers': []})
        before = self.store.events()
        with patch('factory.skill_catalog.discover_catalog', side_effect=AssertionError('Must not scan')):
            repeated = Architecture(self.store, FakeArchitect()).run()
        self.assertEqual(repeated, result)
        self.assertEqual(before, self.store.events())

    def test_human_decision_blocks_and_answer_is_incorporated_on_resume(self):
        initial = proposal(); initial['unresolved_questions'] = [question()]
        result, model = self.run_arch(initial)
        self.assertEqual(result['phase'], 'architecture')
        self.assertEqual(result['status'], 'waiting_for_human')
        self.assertEqual(len(model.contexts), 1)
        self.assertEqual(next_action(result)['action'], 'wait_for_human')
        self.assertEqual(Architecture(self.store, FakeArchitect(), self.router).run(), result)
        decision = result['architecture']['decision_details'][0]
        self.store.answer_decision(decision['id'], 'Shared database', result['revision'])
        final = proposal(); final['proposed_decisions'][0]['human_key'] = question()['key']
        result, model = self.run_arch(final, review())
        self.assertEqual(result['phase'], 'planning')
        self.assertEqual(model.contexts[0]['human_answers'][0]['answer'], 'Shared database')
        self.assertEqual(result['architecture']['adrs'][0]['human_decision_id'], decision['id'])

    def test_invalid_contract_and_interrupt_are_durable_retryable(self):
        for failure in ({'done': True}, KeyboardInterrupt()):
            with self.assertRaises((WorkflowError, KeyboardInterrupt)):
                self.run_arch(failure)
            current = self.store.snapshot()['architecture']
            self.assertEqual(current['stage'], 'propose')
            self.assertIsNone(current['baseline'])
            self.assertTrue(current['blockers'])
        result, _ = self.run_arch(proposal(), review())
        self.assertEqual(result['phase'], 'planning')
        self.assertEqual(result['architecture']['calls'], 4)

    def test_skills_routing_uses_only_selected_and_required_native_inputs(self):
        self.router = SkillRouter(Catalog(tuple(Skill(s, s, '/skills/' + s + '/SKILL.md') for s in
            ('api-and-interface-design', 'archify', 'security-and-hardening', 'team-policy', 'unrelated'))),
            Policy(required=('team-policy',)))
        result, model = self.run_arch(proposal(), review())
        for context, inputs in zip(model.contexts, model.inputs):
            self.assertIn('Required skills (policy): team-policy', context['skills'])
            self.assertIn('Recommended skills:', context['skills'])
            self.assertNotIn('unrelated', json.dumps(context))
            self.assertNotIn('archify', context['skills'])
            self.assertEqual(inputs, [{'type': 'skill', 'name': 'team-policy', 'path': '/skills/team-policy/SKILL.md'}])
        self.assertEqual(result['phase'], 'planning')

    def test_critical_security_classification_activates_required_router_rule(self):
        source = source_snapshot(self.store.snapshot())
        next(f for f in source['knowledge'] if f['category'] == 'security')['text'] = 'HIPAA patient isolation'
        self.assertEqual(classify(source)['risk'], 'critical')
        with patch('factory.architecture.source_snapshot', return_value=source):
            with self.assertRaisesRegex(WorkflowError, 'critical-security'):
                self.run_arch()
        self.assertEqual(self.store.snapshot()['architecture']['calls'], 0)

    def test_missing_required_skill_blocks_before_paid_call_and_can_resume(self):
        self.router = SkillRouter(Catalog(), Policy(required=('missing',)))
        model = FakeArchitect()
        with self.assertRaisesRegex(WorkflowError, 'routing is blocked'):
            Architecture(self.store, model, self.router).run()
        self.assertEqual(model.contexts, [])
        self.assertEqual(self.store.snapshot()['architecture']['calls'], 0)
        self.assertTrue(self.store.snapshot()['architecture']['blockers'])
        self.router = SkillRouter(Catalog())
        result, _ = self.run_arch(proposal(), review())
        self.assertEqual(result['phase'], 'planning')

    def test_real_catalog_discovery_and_project_override_are_integrated(self):
        (self.project / '.factory' / 'skills.json').write_text(json.dumps({'policy': {'required': ['policy']}}))
        cat = Catalog((Skill('policy', 'policy', '/local/SKILL.md'),))
        model = FakeArchitect(proposal(), review())
        with patch('factory.skill_catalog.discover_catalog', return_value=cat) as discover:
            Architecture(self.store, model).run()
        discover.assert_called_once_with(self.project, ())
        self.assertEqual(model.inputs[0][0]['path'], '/local/SKILL.md')

    def test_bounded_review_and_reconciliation(self):
        result, model = self.run_arch(proposal(), review([finding()]), proposal(), review())
        self.assertEqual([c['role'] for c in model.contexts], ['propose', 'critic', 'reconcile', 'critic_final'])
        self.assertEqual(result['phase'], 'planning')
        self.assertEqual(len(result['architecture']['baseline']['review']['passes']), 2)

    def test_remaining_disagreement_becomes_human_block_and_acceptance_never_reinvokes_model(self):
        result, model = self.run_arch(proposal(), review([finding()]), proposal(), review([finding()]))
        self.assertEqual(result['phase'], 'architecture')
        self.assertEqual(len(model.contexts), 4)
        pending = result['architecture']['decision_details'][0]
        self.store.answer_decision(pending['id'], 'accept', result['revision'])
        result, model = self.run_arch()
        self.assertEqual(result['phase'], 'planning')
        self.assertEqual(model.contexts, [])
        self.assertTrue(result['architecture']['baseline']['review']['human_decisions'])

    def test_gate_can_repair_once_but_never_loop(self):
        incomplete = proposal(); incomplete['testing'] = []
        result, model = self.run_arch(incomplete, review(), proposal(), review())
        self.assertEqual(result['phase'], 'planning')
        self.assertEqual(len(model.contexts), 4)
        self.assertIn('testing_missing', model.contexts[2]['gate_errors'])
        self.assertEqual(result['architecture']['reconciliations'], 1)

    def test_critic_structural_contradiction_cannot_be_waived_by_acceptance(self):
        result, _ = self.run_arch(proposal(), review([finding('inconsistency')]), proposal(), review([finding('inconsistency')]))
        self.store.answer_decision(result['decisions'][0]['id'], 'accept', result['revision'])
        result, model = self.run_arch()
        self.assertEqual(result['phase'], 'architecture')
        self.assertEqual(result['architecture']['stage'], 'blocked')
        self.assertEqual(model.contexts, [])
        self.assertIn('unresolved_review_inconsistency:capacity_risk', result['architecture']['blockers'])

    def test_dependency_contract_storage_and_flow_validation(self):
        value = proposal()
        component = deepcopy(value['components'][0]); component['id'] = 'worker'
        value['components'].append(component)
        value['contracts'] = [{'id': 'job_contract', 'description': 'Jobs', 'participants': ['app', 'worker'],
                              'protocol': 'Durable queue', 'invariants': ['Idempotent processing']}]
        value['dependencies'] = [{'id': 'dispatch', 'source': 'app', 'target': 'worker', 'purpose': 'Dispatch work', 'contract': 'job_contract'}]
        value['information_flows'][0]['steps'] = ['app', 'worker']
        for section in ('testing', 'runtime'):
            value[section][0]['components'].append('worker')
        value['data'] = [{'id': 'jobs', 'owner': 'worker', 'entities': ['Job belongs to user'], 'storage': 'PostgreSQL', 'persistence': 'persistent',
                          'consistency': 'Transactional', 'lifecycle': 'Versioned migrations; 30 day retention'}]
        storage = deepcopy(value['proposed_decisions'][0]); storage.update(id='storage_choice', kind='storage')
        value['proposed_decisions'].append(storage)
        source = source_snapshot(self.store.snapshot())
        self.assertTrue(quality_gate(value, source, [], True)['passed'])
        value['information_flows'][0]['steps'] = ['app', 'worker', 'app']
        self.assertTrue(quality_gate(value, source, [], True)['passed'])
        value['data'][0]['persistence'] = 'transient'
        value['proposed_decisions'].pop()
        self.assertTrue(quality_gate(value, source, [], True)['passed'])
        value['data'][0]['persistence'] = 'persistent'
        self.assertIn('storage_adr_missing', quality_gate(value, source, [], True)['errors'])
        value['contracts'][0]['participants'] = ['app']
        self.assertIn('dependency_contract:dispatch', quality_gate(value, source, [], True)['errors'])
        value['dependencies'] = []
        self.assertIn('flow_dependency:main_flow', quality_gate(value, source, [], True)['errors'])

    def test_process_lock_prevents_duplicate_model_calls(self):
        engine = Architecture(self.store, FakeArchitect(), self.router)
        with engine._lock(), self.assertRaisesRegex(WorkflowError, 'another process'):
            self.run_arch(proposal())

    def test_abrupt_process_exit_resumes_without_losing_budget_or_replaying_checkpoints(self):
        code = """
import os,sys
from factory.architecture import Architecture
from factory.workflow import Store
from factory.skill_router import SkillRouter
from factory.skill_catalog import Catalog
from tests.architecture_fakes import FakeArchitect
Architecture(Store(sys.argv[1]), FakeArchitect(lambda _: os._exit(7)), SkillRouter(Catalog())).run()
"""
        process = subprocess.run([sys.executable, '-c', code, str(self.project)], capture_output=True, text=True)
        self.assertEqual(process.returncode, 7, process.stderr)
        self.assertEqual(self.store.snapshot()['architecture']['calls'], 1)
        result, model = self.run_arch(proposal(), review())
        self.assertEqual(result['phase'], 'planning')
        self.assertEqual(result['architecture']['calls'], 3)
        self.assertEqual(len(model.contexts), 2)
        with sqlite3.connect(self.store.path) as db:
            self.assertEqual(db.execute('SELECT status FROM architecture_calls ORDER BY id LIMIT 1').fetchone()[0], 'interrupted')

    def test_context_overflow_does_not_silently_truncate(self):
        with patch('factory.architecture.MAX_CONTEXT_BYTES', 100), self.assertRaisesRegex(WorkflowError, 'budget'):
            self.run_arch()
        self.assertEqual(self.store.snapshot()['architecture']['calls'], 0)

    def test_rejected_disagreement_stays_blocked_without_another_review(self):
        result, _ = self.run_arch(proposal(), review([finding()]), proposal(), review([finding()]))
        self.store.answer_decision(result['decisions'][0]['id'], 'reject', result['revision'])
        result, model = self.run_arch()
        self.assertEqual(result['architecture']['stage'], 'blocked')
        self.assertEqual(model.contexts, [])
        self.assertEqual(self.run_arch()[0], result)

    def test_attempt_budget_persists_across_failed_process_runs(self):
        for _ in range(MAX_CALLS):
            with self.assertRaisesRegex(WorkflowError, 'expected|fields'):
                self.run_arch({})
        result, model = self.run_arch()
        self.assertEqual(model.contexts, [])
        self.assertEqual(result['architecture']['stage'], 'blocked')
        self.assertIn('limit', result['architecture']['blockers'][0])

    def test_trivial_local_application_skips_critic(self):
        with tempfile.TemporaryDirectory() as path:
            store = Store(path); store.initialize()
            reply = complete_reply()
            for fact in reply['knowledge']:
                fact['text'] = 'Uso personal offline para un usuario' if fact['category'] != 'integrations' else 'ninguna'
            Discovery(store, FakeModel(reply)).submit('Small local utility')
            model = FakeArchitect(proposal(reply['knowledge']))
            result = Architecture(store, model, self.router).run()
            self.assertEqual(result['phase'], 'planning')
            self.assertEqual(len(model.contexts), 1)
            self.assertFalse(result['architecture']['classification']['required'])

    def test_simplification_cannot_downgrade_a_required_review(self):
        source = source_snapshot(self.store.snapshot())
        for fact in source['knowledge']:
            fact['text'] = 'Uso personal offline para un usuario' if fact['category'] != 'integrations' else 'ninguna'
        initial = proposal(); initial['risks'] = [{'id': 'peak_risk', 'severity': 'high', 'description': 'Unexpected runtime size',
                                                 'mitigation': 'Simplify the design', 'acceptance_key': ''}]
        with patch('factory.architecture.source_snapshot', return_value=source):
            result, model = self.run_arch(initial, review([finding()]), proposal(), review())
        self.assertEqual([c['role'] for c in model.contexts], ['propose', 'critic', 'reconcile', 'critic_final'])
        self.assertTrue(result['architecture']['classification']['required'])
        self.assertEqual(result['architecture']['classification']['risk'], 'high')
        self.assertIn('previously_required_review', result['architecture']['classification']['reasons'])

    def test_quality_gate_rejects_incomplete_or_incoherent_outputs(self):
        source = source_snapshot(self.store.snapshot())
        mutations = {
            'uncovered_requirement:vision': lambda a: a['coverage'].pop(next(i for i, c in enumerate(a['coverage']) if c['source_key'] == 'vision')),
            'testing_missing': lambda a: a.update(testing=[]),
            'structural_contradictions': lambda a: a.update(contradictions=['Conflicting ownership']),
            'security_coverage:security': lambda a: next(c for c in a['coverage'] if c['source_key'] == 'security').update(targets=['app']),
            'critical_risk:loss': lambda a: a.update(risks=[{'id': 'loss', 'severity': 'critical', 'description': 'Data loss', 'mitigation': '', 'acceptance_key': ''}]),
            'component_reference:runtime_policy': lambda a: a['runtime'][0].update(components=['ghost']),
            'style_adr_missing': lambda a: a.update(proposed_decisions=[]),
        }
        for error, mutate in mutations.items():
            with self.subTest(error=error):
                value = proposal(); mutate(value)
                gate = quality_gate(value, source, [], True)
                self.assertFalse(gate['passed'])
                self.assertIn(error, gate['errors'])
        value = proposal(); value['testing'] = []
        result, _ = self.run_arch(value, review(), value, review())
        self.assertEqual(result['phase'], 'architecture')
        self.assertIsNone(result['architecture']['baseline'])
        with self.assertRaisesRegex(WorkflowError, 'quality gate'):
            self.store.transition('planning', result['revision'])

    def test_assumptions_and_pending_decisions_are_checked_by_code(self):
        source = source_snapshot(self.store.snapshot())
        source['knowledge'][0]['status'] = 'assumption'
        gate = quality_gate(proposal(), source, [{'id': 100, 'answer': None}], True)
        self.assertIn('unrecorded_assumption:' + source['knowledge'][0]['key'], gate['errors'])
        self.assertIn('pending_human_decisions', gate['errors'])
        self.assertIn('review_not_passed', quality_gate(proposal(), source, [], False)['errors'])

    def test_fingerprint_is_canonical_sensitive_and_distinct_from_workflow_revision(self):
        value = proposal()
        self.assertEqual(fingerprint(value), fingerprint(dict(reversed(list(value.items())))))
        modified = deepcopy(value); modified['components'][0]['boundary'] = 'Different ownership'
        self.assertNotEqual(fingerprint(value), fingerprint(modified))
        result, _ = self.run_arch(value, review())
        self.assertNotEqual(result['revision'], result['architecture']['baseline']['revision'])

    def test_minor_decisions_are_not_adrs(self):
        value = proposal(); minor = deepcopy(value['proposed_decisions'][0]); minor.update(id='minor', kind='minor')
        value['proposed_decisions'].append(minor)
        result, _ = self.run_arch(value, review())
        self.assertEqual(len(result['architecture']['adrs']), 1)
        self.assertEqual(len(result['architecture']['baseline']['architecture']['proposed_decisions']), 2)

    def test_stale_call_cannot_override_human_decision(self):
        def concurrent(_):
            self.store.request_decision('New requirement?', self.store.snapshot()['revision'])
            return proposal()
        with self.assertRaisesRegex(WorkflowError, 'Stale revision'):
            self.run_arch(concurrent)
        result = self.store.snapshot()
        self.assertIsNone(result['architecture']['proposal'])
        self.assertEqual(result['status'], 'waiting_for_human')

    def test_completion_and_adrs_roll_back_together_on_event_failure(self):
        def final(_):
            with sqlite3.connect(self.store.path) as db:
                db.execute("""CREATE TRIGGER fail_completion BEFORE INSERT ON events
                    WHEN NEW.kind='architecture_completed' BEGIN SELECT RAISE(ABORT, 'failure'); END""")
            return review()
        with self.assertRaises(sqlite3.IntegrityError):
            self.run_arch(proposal(), final)
        state = self.store.snapshot()
        self.assertEqual(state['architecture']['stage'], 'gate')
        self.assertEqual(state['architecture']['adrs'], [])
        self.assertIsNone(state['architecture']['baseline'])
        with sqlite3.connect(self.store.path) as db:
            db.execute('DROP TRIGGER fail_completion')
        result, model = self.run_arch()
        self.assertEqual(result['phase'], 'planning')
        self.assertEqual(model.contexts, [])

    def test_cli_queries_and_answer_work_across_processes_without_sdk(self):
        initial = proposal(); initial['unresolved_questions'] = [question()]
        result, _ = self.run_arch(initial)
        def cli(*args):
            return subprocess.run([sys.executable, '-m', 'factory', args[0], str(self.project), *args[1:]], capture_output=True, text=True)
        status = cli('architecture-status')
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertTrue(json.loads(status.stdout)['pending_decisions'])
        answer = cli('answer', '--id', str(result['decisions'][0]['id']), '--answer', 'Shared database')
        self.assertEqual(answer.returncode, 0, answer.stderr)
        value = proposal(); value['proposed_decisions'][0]['human_key'] = question()['key']
        self.run_arch(value, review())
        self.assertEqual(json.loads(cli('architecture-show').stdout)['revision'], 1)
        self.assertIn('Architecture revision 1', cli('architecture-show', '--markdown').stdout)
        self.assertEqual(len(json.loads(cli('architecture-adrs').stdout)), 1)
        self.assertEqual(cli('architecture').returncode, 0)

    def test_v2_migration_is_additive_and_read_only_queries_do_not_migrate(self):
        with sqlite3.connect(self.store.path) as db:
            for table in ('architecture_changes', 'architecture_adrs', 'architecture_current', 'architecture_baselines',
                          'architecture_run', 'architecture_calls', 'architecture_decisions'):
                db.execute('DROP TABLE ' + table)
            db.execute('PRAGMA user_version=2')
        before = self.store.events()
        self.assertEqual(self.store.snapshot()['architecture']['stage'], 'not_started')
        with sqlite3.connect(self.store.path) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 2)
        self.store.initialize()
        self.assertEqual(before, self.store.events())
        self.assertEqual(self.run_arch(proposal(), review())[0]['phase'], 'planning')


class ArchitectureAdapterTests(unittest.TestCase):
    def sdk(self):
        codex = MagicMock()
        codex.account.return_value.account = SimpleNamespace(root=SimpleNamespace(type='chatgpt'))
        codex.thread_start.return_value.run.return_value = SimpleNamespace(status='completed', final_response=json.dumps(proposal()))
        factory = MagicMock(); factory.return_value.__enter__.return_value = codex
        sdk = SimpleNamespace(Codex=factory, CodexConfig=lambda **kw: SimpleNamespace(**kw),
              Sandbox=SimpleNamespace(read_only='read-only'), ApprovalMode=SimpleNamespace(deny_all='deny_all'),
              SkillInput=lambda **kw: SimpleNamespace(kind='skill', **kw), TextInput=lambda text: SimpleNamespace(kind='text', text=text))
        return sdk, codex

    def test_native_skill_input_and_fresh_independent_critic(self):
        sdk, codex = self.sdk()
        with patch.dict(sys.modules, {'openai_codex': sdk}):
            adapter = CodexArchitecture()
            adapter.respond({'role': 'propose'}, skill_inputs=[{'type': 'skill', 'name': 'policy', 'path': '/local/SKILL.md'}])
            inputs = codex.thread_start.return_value.run.call_args.args[0]
            self.assertEqual(inputs[1].kind, 'skill')
            self.assertEqual(inputs[1].path, '/local/SKILL.md')
            self.assertEqual(codex.thread_start.return_value.run.call_args.kwargs['output_schema'], ARCHITECTURE_SCHEMA)
            adapter.respond({'role': 'critic'}, skill_inputs=[])
        self.assertEqual(codex.thread_start.call_count, 2)
        self.assertEqual(codex.thread_start.return_value.run.call_args.kwargs['output_schema'], REVIEW_SCHEMA)
        self.assertTrue(codex.thread_start.call_args.kwargs['ephemeral'])
        self.assertEqual(codex.thread_start.call_args.kwargs['sandbox'], 'read-only')
        self.assertEqual(sdk.Codex.call_args.kwargs['config'].env['OPENAI_API_KEY'], '')

    def test_api_key_account_or_incomplete_turn_fails_closed(self):
        sdk, codex = self.sdk()
        with patch.dict(sys.modules, {'openai_codex': sdk}):
            codex.account.return_value.account.root.type = 'apiKey'
            with self.assertRaisesRegex(WorkflowError, 'API-key'):
                CodexArchitecture().respond({'role': 'propose'}, skill_inputs=[])
            codex.thread_start.assert_not_called()
            codex.account.return_value.account.root.type = 'chatgpt'
            codex.thread_start.return_value.run.return_value.status = 'interrupted'
            with self.assertRaisesRegex(WorkflowError, 'did not complete'):
                CodexArchitecture().respond({'role': 'propose'}, skill_inputs=[])


if __name__ == '__main__':
    unittest.main()
