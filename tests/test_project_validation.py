import tempfile
import unittest
import json
import os
import subprocess
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from factory.continuation_store import ContinuationStore
from factory.execution_store import ExecutionStore
from factory.milestone_store import MilestoneStore
from factory.project_store import ProjectStore
from factory.project_validation import ProjectGate
from factory.registry import FactoryError
from factory.runtime import Runtime
from factory.verification import Verifier
from tests.project_fakes import setup_project


class ProjectValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def product(self, **kwargs):
        self.service, self.store, self.jobs, self.git, self.sdk, self.refiner = setup_project(self.root, **kwargs)
        self.groups = ContinuationStore(self.store)
        self.journal = ProjectStore(self.store)
        self.executions = ExecutionStore(self.store)

    def run_product(self):
        self.service.execute_next_slice(request_id='project-once')
        self.service.run_pending(*self.jobs[-1])
        return self.groups.inspect()

    def resume(self):
        self.service.resume(); self.service.run_pending(*self.jobs[-1])
        return self.groups.inspect()

    def test_closed_milestones_fail_real_clean_entry_then_repair_and_accept_new_candidate(self):
        self.product()
        remote = self.root / 'remote.git'
        subprocess.run(['git', 'init', '-q', '--bare', str(remote)], check=True)
        self.git('remote', 'add', 'origin', str(remote))
        head = self.git('rev-parse', 'HEAD')
        data = self.run_product()
        self.assertEqual(data['state'], 'project_verified', data)
        milestones = MilestoneStore(self.store).receipts()
        self.assertEqual(len(milestones), 2)
        validations = self.journal.rows('milestone_validations')
        self.assertEqual([v['state'] for v in validations], ['failed', 'verified'])
        first, last = validations
        self.assertNotEqual(first['binding']['commit'], last['binding']['commit'])
        self.assertEqual(milestones[-1]['commit'], first['binding']['commit'])
        entry = next(e for e in first['evidence'] if e['check_id'] == 'entry')
        self.assertEqual(entry['status'], 'FAIL')
        self.assertIn('Product entry failed', entry['log'])
        self.assertEqual(self.sdk.contexts[-1]['failure']['kind'], 'project_integration_failure')
        self.assertEqual(data['budget']['remediation_calls'], 1)
        receipt = self.journal.receipts()[0]
        self.assertEqual(receipt['commit'], self.git('rev-parse', 'factory/accepted'))
        self.assertEqual(receipt['reproducibility']['status'], 'PASS')
        self.assertEqual(self.git('rev-parse', 'HEAD'), head)
        self.assertTrue(Path(data['delivery']).is_file())
        self.assertFalse((Path(data['delivery']).parent / 'source/.factory').exists())
        self.assertEqual(len(self.jobs), 1)
        self.assertEqual(subprocess.check_output(['git', '--git-dir', str(remote), 'for-each-ref']), b'')
        self.assertTrue(all(not r['pending'] for r in self.service.inspect(view='requirements')['requirements']))

    def test_checks_without_model_calls_and_request_repetition(self):
        self.product(broken=False, authorized=False, customize_definition=lambda p, d: p['continuation'].update(max_calls=4))
        self.assertEqual(self.run_product()['state'], 'project_ready_for_validation')
        before = self.groups.inspect()
        self.assertEqual(before['budget']['calls_remaining'], 0)
        self.sdk.init_error = AssertionError('Validation must not initialize the model')
        self.service.validate_project(request_id='validate-once')
        original_run = Verifier.run
        observed = []
        def inspect_pending(verifier, *args, **kwargs):
            if not observed:
                status = self.service.inspect(view='project_validation')
                self.assertEqual(status['state'], 'project_validating')
                self.assertTrue(status['pending_criteria'])
                self.assertTrue(status['candidate_commit'])
                observed.append(status)
            return original_run(verifier, *args, **kwargs)
        with patch.object(Verifier, 'run', inspect_pending):
            self.service.run_pending(*self.jobs[-1])
        self.assertEqual(len(observed), 1)
        after = self.groups.inspect()
        self.assertEqual(after['state'], 'project_verified', after)
        self.assertEqual((after['id'], after['deadline'], after['budget']['calls']),
                         (before['id'], before['deadline'], before['budget']['calls']))
        self.service.validate_project(request_id='another-request')
        self.service.execute_next_slice(request_id='execute-again')
        self.service.resume()
        self.assertEqual(len(self.jobs), 2)
        self.assertEqual(len(self.journal.receipts()), 1)

    def test_a_second_real_clean_failure_exhausts_the_global_cycle(self):
        from tests.project_fakes import final_repair
        self.product()
        def repair_depending_on_git(context):
            value = final_repair(context)
            change = value['changes'][0]
            change['content'] = change['content'].replace('print(describe(int(sys.argv[1]), int(sys.argv[2])))',
                'from pathlib import Path\n    print(describe(int(sys.argv[1]), int(sys.argv[2])) if Path(".git").exists() else "wrong")')
            return value
        self.sdk.responses[3] = repair_depending_on_git
        data = self.run_product()
        self.assertEqual(data['diagnostic']['code'], 'remediation_exhausted', data)
        self.assertEqual(len(self.executions.acceptances()), 4)
        validations = self.journal.rows('milestone_validations')
        self.assertEqual([v['state'] for v in validations], ['failed', 'failed'])
        self.assertNotEqual(validations[0]['binding']['commit'], validations[1]['binding']['commit'])
        self.assertEqual(self.journal.receipts(), [])
        calls = len(self.sdk.contexts)
        self.resume()
        self.assertEqual(len(self.sdk.contexts), calls)

    def test_separate_remediation_authorization_reuses_the_saved_failure(self):
        self.product(remediation=False)
        data = self.run_product()
        self.assertEqual(data['diagnostic']['code'], 'final_remediation_not_authorized')
        self.assertEqual(len(self.sdk.contexts), 3)
        previous = self.journal.rows('milestone_validations')[0]
        self.service.validate_project(request_id='authorize-repair', automatic_remediation=True)
        self.service.run_pending(*self.jobs[-1])
        self.assertEqual(self.groups.inspect()['state'], 'project_verified')
        self.assertEqual(len(self.journal.rows('milestone_validations')), 2)
        self.assertEqual(self.journal.rows('milestone_validations')[0]['evidence'], previous['evidence'])

    def test_saved_check_artifact_recovers_crash_before_journal_evidence(self):
        self.product(broken=False)
        original = ProjectStore.save_validation
        failed = False
        def crash(journal, group, data):
            nonlocal failed
            if data['evidence'] and not failed:
                failed = True
                raise RuntimeError('crash after runner result, before SQLite evidence')
            return original(journal, group, data)
        with patch.object(ProjectStore, 'save_validation', crash), self.assertRaises(RuntimeError):
            self.run_product()
        original_run = Verifier.run
        first_id = next((self.store.path.parent / 'projects').glob('*/checks/*.json')).stem
        def no_repeat(verifier, plan, target, definition, *args, **kwargs):
            self.assertNotIn(first_id, [c['id'] for c in definition['checks']])
            return original_run(verifier, plan, target, definition, *args, **kwargs)
        with patch.object(Verifier, 'run', no_repeat):
            self.assertEqual(self.resume()['state'], 'project_verified')

    def test_acceptance_transaction_rolls_back_then_reuses_verification(self):
        self.product(broken=False)
        original = Runtime.event
        def fail(db, kind, data):
            if kind == 'project_verified':
                raise RuntimeError('durable event failed')
            return original(db, kind, data)
        with patch.object(Runtime, 'event', staticmethod(fail)), self.assertRaises(RuntimeError):
            self.run_product()
        self.assertEqual(self.journal.receipts(), [])
        with patch.object(Verifier, 'run', side_effect=AssertionError('Reuse verified evidence')):
            self.assertEqual(self.resume()['state'], 'project_verified')

    def test_post_acceptance_code_is_pending_and_receipt_remains_historical(self):
        self.product(broken=False)
        self.assertEqual(self.run_product()['state'], 'project_verified')
        receipt = self.journal.receipts()[0]
        self.git('update-ref', 'refs/heads/factory/accepted', self.git('rev-parse', 'HEAD'))
        status = self.service.get_status()
        self.assertEqual(status['project_validation']['state'], 'version_pending')
        self.assertFalse(status['project_validation']['validated_version']['current'])
        self.assertEqual(self.journal.receipts(), [receipt])

    def test_post_acceptance_decision_is_not_covered_by_historical_receipt(self):
        self.product(broken=False)
        self.assertEqual(self.run_product()['state'], 'project_verified')
        receipt = self.journal.receipts()[0]
        decision = self.store.request_decision('A new delivery condition needs review', self.store.snapshot()['revision'])
        self.assertFalse(self.journal.inspect()['validated_version']['current'])
        self.store.answer_decision(decision, 'accept', self.store.snapshot()['revision'])
        self.assertFalse(self.journal.inspect()['validated_version']['current'])
        self.assertEqual(self.journal.receipts(), [receipt])

    def test_declared_dependency_unavailable_is_environment_block_not_product_repair(self):
        self.product(broken=False)
        path = self.store.project / 'test_entry.py'
        path.write_text(path.read_text() + '\n    def test_dependency(self):\n        import unavailable_pilot_dependency\n')
        self.git('add', 'test_entry.py'); self.git('commit', '-qm', 'Predeclared unavailable verification capability')
        data = self.run_product()
        self.assertEqual(data['state'], 'validation_pending', data)
        self.assertEqual(len(self.sdk.contexts), 3)
        self.assertEqual(self.journal.issues()[0]['classification'], 'infrastructure')

    def test_missing_delivery_document_blocks_before_final_pass(self):
        self.product(broken=False, customize_definition=lambda p, d: d['project_acceptance']['delivery_paths'].append('MISSING.md'))
        data = self.run_product()
        self.assertEqual(data['state'], 'validation_pending', data)
        self.assertEqual(data['diagnostic']['classification'], 'delivery_gap')
        self.assertEqual(self.journal.receipts(), [])

    def test_concurrent_ref_change_before_publication_invalidates_pass(self):
        self.product(broken=False)
        original = ProjectGate.publish
        def concurrent(gate, validation, checks):
            self.git('update-ref', 'refs/heads/factory/accepted', self.git('rev-parse', 'HEAD'))
            return original(gate, validation, checks)
        with patch.object(ProjectGate, 'publish', concurrent):
            data = self.run_product()
        self.assertEqual(data['diagnostic']['code'], 'integration_conflict', data)
        self.assertEqual(self.journal.receipts(), [])

    def test_missing_transversal_acceptance_never_inferred_from_receipts(self):
        self.product(broken=False, customize_definition=lambda p, d: d.pop('requirement_acceptance'))
        data = self.run_product()
        self.assertEqual(data['state'], 'validation_pending', data)
        self.assertTrue(any(p.startswith('full_requirement_acceptance_missing:') for p in data['diagnostic']['pending']))
        self.assertEqual(len(MilestoneStore(self.store).receipts()), 2)
        self.assertEqual(self.journal.receipts(), [])

    def test_recovery_after_verification_reuses_evidence(self):
        self.product(broken=False)
        with patch.object(ProjectGate, 'publish', side_effect=RuntimeError('crash before receipt')), self.assertRaises(RuntimeError):
            self.run_product()
        with patch.object(Verifier, 'run', side_effect=AssertionError('Current evidence must be reused')):
            data = self.resume()
        self.assertEqual(data['state'], 'project_verified', data)
        self.assertEqual(len(self.journal.receipts()), 1)

    def test_residual_untracked_file_cannot_hide_real_entry_failure(self):
        self.product(broken=False, remediation=False)
        script = self.sdk.responses[2]['changes'][0]
        script['content'] = script['content'].replace('print(describe(int(sys.argv[1]), int(sys.argv[2])))',
            'from pathlib import Path\n    print("sum=2" if Path("residual.fixture").exists() else "wrong")')
        (self.store.project / 'residual.fixture').write_text('developer residue')
        data = self.run_product()
        self.assertEqual(data['diagnostic']['code'], 'final_remediation_not_authorized', data)
        validation = self.journal.rows('milestone_validations')[0]
        self.assertFalse((Path(validation['worktree']) / 'residual.fixture').exists())
        self.assertEqual(next(e for e in validation['evidence'] if e['check_id'] == 'entry')['status'], 'FAIL')
        # The same candidate's CLI would appear to pass in a dirty developer checkout.
        worktree = Path(self.executions.latest()['worktree'])
        (worktree / 'residual.fixture').write_text('residue')
        observed = subprocess.check_output(['/usr/bin/python3', '-S', 'final.py', '-1', '2'], cwd=worktree,
                                           env={'PATH': '/usr/bin:/bin'}, text=True)
        self.assertEqual(observed, 'sum=2\n')
        self.assertEqual(self.journal.receipts(), [])

    def test_clean_environment_has_no_venv_factory_state_secrets_or_residual_database(self):
        self.product(broken=False)
        test = self.store.project / 'test_entry.py'
        test.write_text(test.read_text() + '''
    def test_environment(self):
        import os, sys, sysconfig, ssl, sqlite3, zlib, bz2, lzma
        from pathlib import Path
        self.assertNotIn('FACTORY_TEST_SECRET', os.environ)
        self.assertFalse(Path('/home/personal.db').exists())
        self.assertFalse(Path('.factory').exists())
        self.assertFalse(Path('.venv').exists())
        self.assertFalse(Path('/usr/bin/git').exists())
        self.assertFalse(Path('/bin/sh').exists())
        self.assertFalse(Path('/usr/local').exists())
        native = Path('/usr/lib') / sysconfig.get_config_var('MULTIARCH')
        self.assertFalse(any(p.is_dir() for p in native.iterdir()))
        self.assertEqual(sqlite3.connect(':memory:').execute('select 1').fetchone(), (1,))
        for codec in (zlib, bz2, lzma):
            self.assertEqual(codec.decompress(codec.compress(b'fixture')), b'fixture')
        self.assertEqual(sys.prefix, sys.base_prefix)
        self.assertFalse(any('site-packages' in p or 'dist-packages' in p for p in sys.path))
''')
        self.git('add', 'test_entry.py'); self.git('commit', '-qm', 'Approved clean environment assertions')
        (self.store.project / '.venv').mkdir()
        (self.store.project / 'personal.db').write_text('residual')
        with patch.dict(os.environ, {'FACTORY_TEST_SECRET': 'not-for-child'}):
            data = self.run_product()
        self.assertEqual(data['state'], 'project_verified', data)

    def test_unsupported_mandatory_final_check_blocks_after_both_closures(self):
        def configure(p, d):
            extra = deepcopy(next(c for c in d['checks'] if c['id'] == 'entry'))
            extra.update(id='specialist', kind='specialist', target='external audit')
            extra.pop('entrypoint')
            d['checks'].append(extra)
        self.product(broken=False, customize_definition=configure)
        data = self.run_product()
        self.assertEqual(data['diagnostic']['code'], 'capability_unavailable', data)
        self.assertEqual(len(MilestoneStore(self.store).receipts()), 2)
        self.assertEqual(self.journal.receipts(), [])
        self.assertEqual(len(self.sdk.contexts), 3)

    def test_structural_final_repair_stops_at_existing_proposal_boundary(self):
        from tests.project_fakes import final_repair
        self.product()
        baseline = self.store.snapshot()['architecture']['baseline']
        def structural(context):
            value = final_repair(context)
            value['architecture'] = {'impact': 'proposal', 'reason': 'Correction requires a different dependency contract', 'references': ['app']}
            return value
        self.sdk.responses[3] = structural
        data = self.run_product()
        self.assertEqual(data['state'], 'blocked', data)
        self.assertEqual(self.journal.receipts(), [])
        self.assertEqual(self.journal.issues()[0]['classification'], 'architecture_change')
        self.assertEqual(self.store.snapshot()['architecture']['baseline'], baseline)

    def test_pending_smoke_contract_is_extended_without_model_or_budget_changes(self):
        from scripts.execution_smoke_fixture import prepare, upgrade_prepared
        from factory.execution_contract import validate_verification
        from factory.workflow import Store
        with tempfile.TemporaryDirectory() as tmp:
            prepared = prepare(Path(tmp), 'test-model', 'low', continuation=True)
            before = deepcopy(prepared['policy']['continuation'])
            upgraded = upgrade_prepared(prepared)
            snapshot = Store(upgraded['project']['path']).snapshot()
            validate_verification(upgraded['verification'], snapshot['planning']['roadmap']['plan'])
            self.assertEqual(upgraded['policy']['continuation'], before)
            self.assertEqual(upgraded['policy']['quota_reserve_percent'], 25)
            self.assertTrue(upgraded['policy']['final_validation']['enabled'])
            self.assertFalse(upgraded['policy']['final_validation']['automatic_remediation'])
            self.assertIsNone(ContinuationStore(Store(upgraded['project']['path'])).latest())

    def test_simulated_integration_is_explicit_in_receipt_and_report(self):
        def configure(p, d):
            next(c for c in d['checks'] if c['id'] == 'entry')['integration_mode'] = 'simulated'
        self.product(broken=False, customize_definition=configure)
        data = self.run_product()
        self.assertEqual(data['state'], 'project_verified', data)
        receipt = self.journal.receipts()[0]
        self.assertIn('Simulated integration: entry', receipt['limitations'])
        self.assertIn('simulated', Path(data['delivery']).read_text())

    def test_human_approval_becomes_obsolete_after_final_remediation(self):
        def configure(p, d):
            human = deepcopy(next(c for c in d['checks'] if c['id'] == 'entry'))
            human.update(id='human', kind='human_review', target='Review the approved subjective clarity condition')
            human.pop('entrypoint')
            d['checks'].append(human)
        self.product(customize_definition=configure)
        self.assertEqual(self.run_product()['state'], 'waiting_decision')
        first = self.store.snapshot()['decisions'][-1]
        self.service.answer_decision(first['id'], 'accept'); self.service.run_pending(*self.jobs[-1])
        data = self.groups.inspect()
        self.assertEqual(data['state'], 'waiting_decision', data)
        second = self.store.snapshot()['decisions'][-1]
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual(self.journal.receipts(), [])
        self.service.answer_decision(second['id'], 'accept'); self.service.run_pending(*self.jobs[-1])
        self.assertEqual(self.groups.inspect()['state'], 'project_verified')
        receipt = self.journal.receipts()[0]
        self.assertEqual([a['id'] for a in receipt['approvals']], [second['id']])
        self.assertIn(receipt['commit'], second['question'])

    def test_code_modification_after_verification_blocks_acceptance(self):
        self.product(broken=False)
        original = ProjectGate.publish
        def changed(gate, validation, checks):
            (Path(validation['worktree']) / 'app.py').write_text('def add(a,b): return 99\n')
            return original(gate, validation, checks)
        with patch.object(ProjectGate, 'publish', changed):
            data = self.run_product()
        self.assertEqual(data['state'], 'validation_pending', data)
        self.assertEqual(self.journal.receipts(), [])

    def test_original_criteria_and_oracles_cannot_be_weakened_during_closure(self):
        self.product()
        self.sdk.responses[3] = lambda context: {**deepcopy(self.sdk.contexts[0]), 'changes': []}
        original = ProjectGate.publish
        def reconfigure(gate, validation, checks):
            policy = {k: v for k, v in self.executions.policy().items() if k not in ('repository', 'definition_id', 'authorized_at')}
            definition = deepcopy(gate.definition)
            definition['checks'].clear()
            with self.assertRaises(FactoryError):
                self.service.configure_execution(policy, definition)
            return original(gate, validation, checks)
        # Exercise successful publication; a model never receives a configuration API.
        self.sdk.responses[2]['changes'][0]['content'] = self.sdk.responses[2]['changes'][0]['content'].replace('int(sys.argv[1]), 0', 'int(sys.argv[1]), int(sys.argv[2])')
        with patch.object(ProjectGate, 'publish', reconfigure):
            self.assertEqual(self.run_product()['state'], 'project_verified')

    def test_global_final_cycle_cannot_reset_with_commit_or_failure_name(self):
        self.product()
        self.assertEqual(self.run_product()['state'], 'project_verified')
        data = self.groups.latest()
        from factory.continuation import Continuation
        gate = ProjectGate(Continuation(self.service, self.store), data, self.store.snapshot(),
                           self.executions.definition(data['policy']['definition_id'])['verification'])
        for commit, issue in [('another-commit', 'renamed-failure'), ('same-commit', 'another-variant')]:
            with self.assertRaises(FactoryError) as error:
                gate.remediation_guard({'id': commit}, [{'id': issue}])
            self.assertEqual(error.exception.code, 'remediation_exhausted')
        with self.store._connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM project_remediation').fetchone()[0], 1)

    def test_failure_after_receipt_before_report_recovers_without_rechecking(self):
        self.product(broken=False)
        with patch('factory.project_delivery.deliver', side_effect=OSError('report failure')), self.assertRaises(OSError):
            self.run_product()
        receipt = self.journal.receipts()[0]
        group = self.groups.latest()
        group['deadline'] = 0
        self.groups.save(group)
        with patch.object(Verifier, 'run', side_effect=AssertionError('Receipt already durable')):
            data = self.resume()
        self.assertEqual(data['state'], 'project_verified', data)
        self.assertEqual(self.journal.receipts(), [receipt])
        self.assertTrue(Path(data['delivery']).is_file())
        self.assertEqual(data['deadline'], 0)
        with self.store._connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM factory_control_events WHERE kind='project_verified'").fetchone()[0], 1)

    def test_report_renders_prior_input_authorization_without_a_synthetic_decision(self):
        from factory.project_delivery import deliver
        self.product(broken=False)
        self.run_product()
        receipt = self.journal.receipts()[0]
        projection = deepcopy(receipt)
        projection['exclusions'] = [{'requirement': 'scope', 'disposition': 'out_of_scope',
            'rationale': 'External services excluded in the prior input',
            'authorization': {'prior_input': {'request_id': 'initial-brief', 'quote': 'No external services'}}}]
        with patch.object(Verifier, 'run', side_effect=AssertionError('Report recovery must not run checks')):
            report = Path(deliver(self.store, projection)).read_text()
        self.assertIn('mensaje previo initial-brief', report)
        self.assertNotIn('(decisión', report)
        self.assertEqual(self.journal.receipts(), [receipt])

    def test_prior_authorized_exclusion_preserved_and_late_reclassification_rejected(self):
        def plan(p):
            key = 'out_of_scope'
            c = next(c for c in p['coverage'] if c['requirement'] == key)
            c.update(disposition='out_of_scope', slices=[], rationale='External services were explicitly excluded in the approved scope')
            for s in p['slices']:
                s['requirements'].remove(key)
            p['milestones'][1]['requirements'].remove(key)
        self.product(broken=False, custom=plan)
        # The contract was already frozen without this decision: a later answer cannot
        # retroactively authorize the omission, even when the local checks pass.
        data = self.run_product()
        self.assertEqual(data['state'], 'validation_pending', data)
        self.assertIn('prior_exclusion_authorization_missing:out_of_scope', data['diagnostic']['pending'])
        policy = {k: v for k, v in self.executions.policy().items() if k not in ('repository', 'definition_id', 'authorized_at')}
        definition = self.executions.definition(self.executions.policy()['definition_id'])['verification']
        definition['project_acceptance']['exclusions'] = [{'requirement': 'out_of_scope', 'decision_id': 1}]
        with self.assertRaises(FactoryError):
            self.service.configure_execution(policy, definition)
        self.assertEqual(self.journal.receipts(), [])

    def test_previously_approved_exclusion_survives_receipt_and_delivery(self):
        def plan(p):
            c = next(c for c in p['coverage'] if c['requirement'] == 'out_of_scope')
            c.update(disposition='out_of_scope', slices=[], rationale='External services excluded in discovery')
            for s in p['slices']:
                s['requirements'].remove('out_of_scope')
            p['milestones'][1]['requirements'].remove('out_of_scope')
        self.product(broken=False, custom=plan, authorize_exclusions=True)
        data = self.run_product()
        self.assertEqual(data['state'], 'project_verified', data)
        exclusion = self.journal.receipts()[0]['exclusions'][0]
        self.assertEqual(exclusion['disposition'], 'out_of_scope')
        self.assertEqual(exclusion['authorization']['answer'], 'accept')
        self.assertIn(exclusion['rationale'], Path(data['delivery']).read_text())
        definition = self.executions.definition(self.executions.policy()['definition_id'])['verification']
        definition['requirement_acceptance'][0]['condition'] = 'An easier replacement condition'
        policy = {k: v for k, v in self.executions.policy().items() if k not in ('repository', 'definition_id', 'authorized_at')}
        with self.assertRaises(FactoryError) as error:
            self.service.configure_execution(policy, definition)
        self.assertEqual(error.exception.code, 'acceptance_contract_frozen')

    def test_pause_keeps_checks_and_prevents_final_publication(self):
        self.product(broken=False)
        original = ProjectGate.publish
        def pause(gate, validation, checks):
            self.service.pause()
            return original(gate, validation, checks)
        with patch.object(ProjectGate, 'publish', pause):
            data = self.run_product()
        self.assertEqual(data['state'], 'paused', data)
        self.assertEqual(self.journal.receipts(), [])
        with patch.object(Verifier, 'run', side_effect=AssertionError('Pause must preserve checks')):
            self.assertEqual(self.resume()['state'], 'project_verified')

    def test_final_gate_cannot_extend_deadline_or_aggregate_units(self):
        self.product(customize_definition=lambda p, d: p['continuation'].update(max_slices=3))
        data = self.run_product()
        self.assertEqual(data['state'], 'budget_exhausted', data)
        self.assertEqual(len(self.sdk.contexts), 3)
        self.assertEqual(self.journal.receipts(), [])

    def test_zero_skipped_and_timeout_checks_never_publish(self):
        for source, expected in [('import unittest\n', 'missing_dependency_or_tests'),
            ('import unittest\nclass T(unittest.TestCase):\n @unittest.skip("not available")\n def test_missing(self): pass\n', 'missing_dependency_or_tests'),
            ('import time\ntime.sleep(6)\n', 'timeout')]:
            with self.subTest(reason=expected), tempfile.TemporaryDirectory() as tmp:
                service, store, jobs, git, sdk, _ = setup_project(Path(tmp), broken=False)
                (store.project / 'test_entry.py').write_text(source)
                git('add', 'test_entry.py'); git('commit', '-qm', 'Predeclared unavailable final test')
                service.execute_next_slice(request_id='not-pass'); service.run_pending(*jobs[-1])
                journal = ProjectStore(store)
                self.assertEqual(journal.receipts(), [])
                entry = next(e for e in journal.rows('milestone_validations')[0]['evidence'] if e['check_id'] == 'entry')
                self.assertEqual(entry['status'], 'NOT_RUN')
                self.assertEqual(entry['reason'], expected)
                self.assertEqual(len(sdk.contexts), 3)

    def test_precondition_gate_does_not_invent_additional_slice_acceptance_at_project_close(self):
        def plan(p):
            p['slices'][0]['verification_triggers'] = ['cross_component']
            g = deepcopy(p['gates'][0])
            g.update(id='before_contract', kind='integration', target='s1', trigger='before_slice',
                     checks=['Zero input remains neutral'], signals=['cross_component'], harness=[])
            p['gates'].append(g)
        def definition(p, d):
            c = deepcopy(d['checks'][0])
            c.update(id='before_neutral', gate='before_contract', criteria=[],
                     integration_mode='local', cases=[{'args_json': '[0,0]', 'expected_json': '0'}])
            d['checks'].append(c)
        self.product(broken=False, custom=plan, customize_definition=definition)
        data = self.run_product()
        self.assertEqual(data['state'], 'project_verified', data)
        self.assertEqual(next(e for e in self.journal.receipts()[0]['evidence'] if e['check_id'] == 'before_neutral')['status'], 'PASS')

    def test_old_process_and_concurrent_accepted_reference_cannot_publish(self):
        self.product(broken=False)
        original = ProjectGate.publish
        def superseded(gate, validation, checks):
            Runtime(self.store).queue()
            return original(gate, validation, checks)
        with patch.object(ProjectGate, 'publish', superseded), self.assertRaises(FactoryError):
            self.run_product()
        self.assertEqual(self.journal.receipts(), [])

    def test_quota_reserve_blocks_only_remediation_after_saving_final_failure(self):
        self.product()
        original = ProjectGate.handle_failures
        def no_quota(gate, validation, pending):
            import time
            self.sdk.quota_value = {'observed_at': time.time(), 'buckets': {'codex': {'primary': {'usedPercent': 80, 'resetsAt': time.time()+1000}}}}
            return original(gate, validation, pending)
        with patch.object(ProjectGate, 'handle_failures', no_quota):
            data = self.run_product()
        self.assertEqual(data['state'], 'quota_blocked', data)
        self.assertEqual(data['diagnostic']['code'], 'quota_reserve')
        self.assertEqual(len(self.sdk.contexts), 3)
        self.assertEqual(self.journal.rows('milestone_validations')[0]['state'], 'failed')
        self.assertEqual(data['budget']['remediation_calls'], 0)

    def test_delivery_commands_execute_and_do_not_change_validated_commit(self):
        self.product(broken=False)
        test = self.store.project / 'test_entry.py'
        test.write_text(test.read_text() + '\n    def test_relative_fixture(self):\n        from pathlib import Path\n        self.assertEqual(Path("fixture.txt").read_text(), "approved fixture")\n')
        (self.store.project / 'fixture.txt').write_text('approved fixture')
        self.git('add', 'test_entry.py', 'fixture.txt'); self.git('commit', '-qm', 'Predeclared relative fixture')
        data = self.run_product()
        self.assertEqual(data['state'], 'project_verified', data)
        commit = self.git('rev-parse', 'factory/accepted')
        root = Path(data['delivery']).parent
        blocks = Path(data['delivery']).read_text().split('```bash\n')[1:]
        for block in blocks:
            commands = block.split('```')[0]
            result = subprocess.run(['/bin/bash', '-e', '-c', commands], cwd=root,
                                    env={'PATH': '/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1'}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git('rev-parse', 'factory/accepted'), commit)
        self.assertFalse(any((root / 'source' / x).exists() for x in ('.git', '.factory', '.venv')))
