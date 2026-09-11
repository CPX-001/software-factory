"""Same two-milestone product scenario, now entered through guarded analysis.

All model outputs are simulated. Git, Python checks, closure and delivery are real.
"""
from copy import deepcopy
import asyncio
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from factory.application import FactoryService
from factory.continuation_store import ContinuationStore
from factory.execution_contract import POLICY_SCHEMA
from factory.execution_store import ExecutionStore
from factory.execution_workspace import git, git_bytes
from factory.planning_binding import compile_binding, context
from factory.project_store import ProjectStore
from factory.registry import FactoryError, Registry
from factory.skill_catalog import Catalog
from factory.skill_router import SkillRouter
from tests.architecture_fakes import proposal, review
from tests.discovery_fakes import complete_reply
from tests.execution_fakes import FakeSDK
from tests.project_fakes import setup_project


class AutomaticBindingTests(unittest.TestCase):
    def test_planning_output_schema_preserves_exclusive_approval_alternatives_in_supported_subset(self):
        from factory.analysis_execution import schema_for
        from factory.execution_contract import check_schema
        from factory.discovery_contract import validate
        from factory.workflow import WorkflowError
        _, schema=schema_for('planning',{'role':'propose','automatic_plan_binding':True})
        def inspect(value):
            if isinstance(value,dict):
                self.assertNotIn('oneOf',value); self.assertNotIn('not',value)
                if value.get('type')=='object':
                    self.assertEqual(set(value['required']),set(value['properties']))
                    self.assertFalse(value['additionalProperties'])
                for v in value.values(): inspect(v)
            elif isinstance(value,list):
                for v in value: inspect(v)
        inspect(schema)
        approval=schema['properties']['execution_binding']['properties']['exclusions']['items']
        check_schema({'requirement':'scope','authorization':'initial'},approval)
        check_schema({'requirement':'scope','decision_id':1},approval)
        validate({'requirement':'scope','authorization':'initial'},approval)
        validate({'requirement':'scope','decision_id':1},approval)
        for value in ({'requirement':'scope'}, {'requirement':'scope','authorization':'initial','decision_id':1}):
            with self.assertRaises(FactoryError): check_schema(value,approval)
            with self.assertRaises(WorkflowError): validate(value,approval)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        reference = self.root / 'reference'; reference.mkdir()
        _, source, _, _, worker, _ = setup_project(reference, broken=False)
        self.reference_store = source
        self.plan = deepcopy(source.snapshot()['planning']['roadmap']['plan'])
        self.plan['near_term'] = [s['id'] for s in self.plan['slices']]
        for item in self.plan['slices']:
            item['maturity'] = 'execution_ready'
        source_journal = ExecutionStore(source)
        self.definition = source_journal.definition(source_journal.policy()['definition_id'])['verification']
        self.policy = {k:v for k,v in source_journal.policy().items() if k in POLICY_SCHEMA['properties']}
        self.policy['write_paths'].remove('test_app.py')
        self.policy.update(automatic_plan_binding=True)
        self.policy['continuation'].update(max_slices=3, max_calls=20, max_tokens=50000)
        self.binding = {'definition_id': '', 'checks': [{k:c[k] for k in ('id','gate','criteria','gate_checks')} |
            {'template':c['id']} for c in self.definition['checks']], 'harness': self.definition['harness'],
            'requirements': [{k:c[k] for k in ('requirement','milestones','checks')}
                             for c in self.definition['requirement_acceptance']], 'exclusions': []}
        self.definition.pop('requirement_acceptance')
        for c in self.definition['checks']:
            c.update(gate='pending', criteria=[], gate_checks=[0])
        self.product = self.root / 'product'; self.product.mkdir()
        for name in git(source.project, 'ls-files').splitlines():
            target = self.product / name; target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(git_bytes(source.project, 'show', 'HEAD:' + name))
        git(self.product, 'init', '-q'); git(self.product, 'add', '.')
        git(self.product, '-c','user.name=Fixture','-c','user.email=fixture@localhost','commit','-qm','Independent product inputs')
        self.jobs = []
        self.model = FakeSDK(complete_reply(), lambda c: proposal(c['source']['knowledge']), review(), self.propose, review())
        self.worker = worker
        self.service = FactoryService(Registry(self.root / 'registry'), router=SkillRouter(Catalog()),
            launcher=lambda p,r: self.jobs.append((p,r)), analysis_worker_factory=self.model,
            execution_worker_factory=self.worker)
        self.service.authorize_root(self.root)
        self.pid = self.service.initialize_project(str(self.product))['project']['id']
        self.store = self.service._store(self.pid)

    def propose(self, ctx):
        p = deepcopy(self.plan)
        for item in [p, *p['milestones'], *p['slices']]:
            item['architecture'] = ctx['source']['architecture']
        p['execution_binding'] = deepcopy(self.binding)
        p['execution_binding']['definition_id'] = ctx['automatic_plan_binding']['definition_id']
        return p

    def start(self):
        self.service.configure_execution(self.policy, self.definition, self.pid)
        self.service.submit_user_message('Small add product. External services excluded.', self.pid, request_id='initial-brief')

    def drive(self):
        self.service.run_pending(*self.jobs[-1])
        return self.service.get_status(self.pid)

    def assert_finished(self):
        state = self.service.get_status(self.pid)
        self.assertEqual(state['state'], 'project_verified', state)
        self.assertEqual(len(ExecutionStore(self.store).acceptances()), 3)
        self.assertEqual(len(state['continuation']['closed_milestones']), 2)
        self.assertEqual(len(ProjectStore(self.store).receipts()), 1)
        self.assertTrue(Path(state['project_validation']['delivery']).is_file())
        self.assertEqual(len(self.model.contexts), 5)
        self.assertEqual(len(self.worker.contexts), 3)

    def test_one_dispatch_crosses_analysis_binding_two_milestones_and_final_delivery(self):
        self.start(); initial = self.service.get_status(self.pid)
        self.drive(); self.assert_finished()
        current = self.service.get_status(self.pid)
        self.assertEqual(len(self.jobs), 1)
        self.assertEqual(initial['autonomous_run']['run_id'], current['autonomous_run']['run_id'])
        self.assertEqual(current['continuation']['budget']['calls'], 8)
        self.assertEqual(current['continuation']['budget']['tokens'], 800)
        self.assertEqual(current['continuation']['budget']['analysis_calls'], 5)
        self.assertIn('resources', self.model.contexts[-1]['automatic_plan_binding'])
        self.assertTrue(self.store.snapshot()['planning']['roadmap']['review']['classification']['required'])
        self.service.resume(self.pid)
        self.service.validate_project(self.pid, request_id='again')
        self.assertEqual(len(self.jobs), 1)
        with self.store._connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM factory_control_events WHERE kind='automatic_verification_bound'").fetchone()[0], 1)

    def test_mcp_operator_recovery_after_exhausted_planning_reaches_delivery_after_disconnect(self):
        from mcp import Client
        from factory.mcp_server import build_server
        def invalid(context):
            p = self.propose(context)
            p['risks'] = [{'id':'contract_drift','severity':'medium','description':'Shared contract risk',
                'owner':'app','mitigation':'Use the shared module','acceptance_key':'',
                'validation_slice':p['slices'][0]['id'],'blocks':[]}]
            p['slices'][0]['risks'] = ['contract_drift']
            return p
        self.model.responses[3:] = [invalid, review(), invalid, review(), invalid, review()]
        self.start(); self.drive()
        initial = self.service.get_status(self.pid)
        self.assertEqual(initial['state'], 'blocked')
        self.assertEqual(self.worker.contexts, [])
        def repair(context):
            p = deepcopy(context['proposal']); p['risks'][0]['owner'] = p['slices'][0]['id']
            return p
        self.model.responses = [repair, review()]
        async def recover():
            async with Client(build_server(self.service)) as client:
                plan = (await client.call_tool('factory_inspect', {'project':self.pid,'view':'plan'})).structured_content['data']
                execution = (await client.call_tool('factory_inspect', {'project':self.pid,'view':'execution'})).structured_content['data']
                policy = {k:v for k,v in execution['policy'].items() if k in POLICY_SCHEMA['properties']}
                request = {'project':self.pid,'policy':policy,'verification':execution['definition']['verification'],
                    'planning_recovery':{'request_id':'operator-recovery-once','run_id':initial['autonomous_run']['run_id'],
                        'proposal_fingerprint':plan['proposal_fingerprint'],'reason':'Authorize one correction and independent review within existing aggregate limits'}}
                result = (await client.call_tool('factory_execution_policy', request)).structured_content
                self.assertTrue(result['ok'], result)
                return request
        request = asyncio.run(recover())
        # The client has closed; only the existing detached-work entrypoint drives phases.
        self.drive()
        final = self.service.get_status(self.pid)
        self.assertEqual(final['state'], 'project_verified', final)
        self.assertEqual(final['autonomous_run']['run_id'], initial['autonomous_run']['run_id'])
        self.assertEqual(len(ProjectStore(self.store).receipts()), 1)
        self.assertEqual(len(final['continuation']['closed_milestones']), 2)
        self.assertEqual(final['continuation']['budget']['calls'], 14)
        self.assertEqual(len(self.model.contexts), 11)
        self.assertEqual(len(self.worker.contexts), 3)
        self.service.configure_execution(request['policy'], request['verification'], self.pid,
                                         planning_recovery=request['planning_recovery'])
        self.assertEqual(len(self.jobs), 2)

    def test_critic_can_identify_binding_root_and_check_without_losing_real_findings(self):
        finding = {'id':'mapping_gap','severity':'high','category':'verification',
            'targets':['execution_binding',self.binding['checks'][0]['id']],
            'description':'The claimed behavior lacks evidence','recommendation':'Preserve the acceptance gap'}
        self.model.responses[3:] = [self.propose, review([finding]), self.propose, review([finding]),
                                   self.propose, review([finding])]
        self.start(); state = self.drive()
        self.assertEqual(state['state'], 'blocked')
        self.assertEqual(self.store.snapshot()['planning']['review']['findings'], [finding])
        self.assertIn('Unresolved review mapping_gap', state['blockers'][0])
        self.assertEqual(self.worker.contexts, [])

    def test_missing_bindings_stop_within_shared_budget_before_implementation(self):
        self.policy['continuation']['max_calls'] = 5
        original = self.propose
        def missing(ctx):
            p = original(ctx); p.pop('execution_binding'); return p
        self.model.responses[3] = missing
        self.start()
        with self.assertRaises(FactoryError):
            self.drive()
        self.assertFalse(ExecutionStore(self.store).acceptances())
        self.assertEqual(self.worker.contexts, [])
        self.assertIn('verification_binding_missing', self.store.snapshot()['planning']['gate']['errors'])
        self.assertEqual(self.service.get_status(self.pid)['continuation']['budget']['calls'], 5)

    def test_crash_after_binding_uses_published_contract_without_repeating_analysis(self):
        from factory.execution import configure
        def interrupted(*args, **kwargs):
            configure(*args, **kwargs)
            raise OSError('After binding publication')
        self.start()
        with patch('factory.execution.configure', interrupted), self.assertRaises(OSError):
            self.drive()
        before = ContinuationStore(self.store).latest()
        self.assertEqual(before['state'], 'execution_authorized')
        self.service.resume(self.pid); self.drive(); self.assert_finished()
        after = ContinuationStore(self.store).latest()
        self.assertEqual(before['id'], after['id'])
        self.assertEqual(before['runtime_id'], after['runtime_id'])
        self.assertEqual(before['deadline'], after['deadline'])

    def test_resource_change_after_review_blocks_and_recovery_reuses_analysis(self):
        from factory.planning_binding import adopt
        self.start()
        original = (self.product / 'test_app.py').read_bytes()
        def changed(store, run):
            (self.product / 'test_app.py').write_text('# replaced oracle\n')
            return adopt(store, run)
        with patch('factory.planning_binding.adopt', changed), self.assertRaises(FactoryError) as error:
            self.drive()
        self.assertEqual(error.exception.code, 'verification_weakened')
        self.assertEqual(self.service.get_status(self.pid)['execution']['diagnostic']['code'], 'verification_weakened')
        self.assertEqual(self.worker.contexts, [])
        before = ContinuationStore(self.store).latest()
        (self.product / 'test_app.py').write_bytes(original)
        self.service.resume(self.pid); self.drive(); self.assert_finished()
        after = ContinuationStore(self.store).latest()
        self.assertEqual(before['runtime_id'], after['runtime_id'])
        self.assertEqual(before['deadline'], after['deadline'])

    def test_stale_owner_cannot_publish_binding(self):
        from factory.execution import configure
        from factory.runtime import Runtime
        def superseded(*args, **kwargs):
            Runtime(self.store).queue()
            return configure(*args, **kwargs)
        self.start()
        with patch('factory.execution.configure', superseded), self.assertRaises(FactoryError) as error:
            self.drive()
        self.assertEqual(error.exception.code, 'stale_run')
        self.assertTrue(ContinuationStore(self.store).latest()['analysis'])
        self.assertEqual(self.worker.contexts, [])

    def test_mcp_disconnect_and_reconnect_preserve_the_single_authorized_dispatch(self):
        from mcp import Client
        from factory.mcp_server import build_server
        async def submit():
            async with Client(build_server(self.service)) as client:
                configured = await client.call_tool('factory_execution_policy',
                    {'project':self.pid, 'policy':self.policy, 'verification':self.definition})
                self.assertTrue(configured.structured_content['ok'], configured)
                response = await client.call_tool('factory_message', {'project':self.pid,
                    'message':'Small add product. External services excluded.', 'request_id':'initial-brief'})
                self.assertTrue(response.structured_content['ok'], response)
                return response.structured_content['data']['autonomous_run']['run_id']
        run = asyncio.run(submit())
        # The actual service controller operates after this test client's connection ends.
        self.drive(); self.assert_finished()
        async def reconnect():
            async with Client(build_server(self.service)) as client:
                state = (await client.call_tool('factory_status', {})).structured_content['data']
                self.assertEqual(state['project']['id'], self.pid)
                self.assertEqual(state['autonomous_run']['run_id'], run)
                self.assertEqual(state['state'], 'project_verified')
                repeated = await client.call_tool('factory_message', {'project':self.pid,
                    'message':'Small add product. External services excluded.', 'request_id':'initial-brief'})
                self.assertTrue(repeated.structured_content['ok'])
                conflict = await client.call_tool('factory_message', {'project':self.pid,
                    'message':'Different input', 'request_id':'initial-brief'})
                self.assertEqual(conflict.structured_content['error']['code'], 'request_id_conflict')
        asyncio.run(reconnect())
        self.assertEqual(len(self.jobs), 1)
        self.assertEqual(self.service.get_status(self.pid)['continuation']['budget']['calls'], 8)

    def test_automatic_authorization_rejects_mutable_untracked_or_unsupported_resources(self):
        for defect in ('mutable', 'hash', 'untracked', 'service', 'duplicate'):
            policy, definition = deepcopy(self.policy), deepcopy(self.definition)
            if defect == 'mutable': policy['write_paths'].append('test_app.py')
            if defect == 'hash': definition['resources'] = [{'path':'test_app.py', 'sha256':'0' * 64}]
            if defect == 'untracked':
                (self.product / 'extra.py').write_text('# not accepted\n')
                definition['resources'] = [{'path':'extra.py', 'sha256':hashlib.sha256(b'# not accepted\n').hexdigest()}]
            if defect == 'service': definition['checks'][0]['integration_mode'] = 'external_service'
            if defect == 'duplicate': definition['resources'] = [{'path':'test_app.py', 'sha256':'0' * 64}] * 2
            with self.subTest(defect=defect), self.assertRaises(FactoryError):
                self.service.configure_execution(policy, definition, self.pid)
        self.assertEqual(self.model.contexts, [])
        self.assertEqual(self.jobs, [])

    def test_same_pilot_preparation_exposes_pinned_templates_without_authorizing_inference(self):
        from scripts.execution_smoke_fixture import prepare_from_discovery
        from factory.planning_binding import prepare_templates
        prepared = prepare_from_discovery(self.root / 'records', self.policy['model'], self.policy['effort'],
            registry_home=self.root / 'records-registry', automatic_binding=True)
        service = FactoryService(Registry(prepared['registry_home']))
        state = service.get_status(prepared['project']['id'])
        self.assertEqual(state['state'], 'paused')
        self.assertIsNone(state['autonomous_run']['run_id'])
        self.assertFalse(state['execution']['enabled'])
        value = prepare_templates(service._store(prepared['project']['id']), prepared['policy'], prepared['verification_templates'])
        self.assertEqual(len(value['checks']), 6)
        self.assertEqual(sum(c['min_tests'] for c in value['checks']), 25)
        self.assertTrue(all(c['clean_copy'] and c['source_sha256'] for c in value['checks']))
        self.assertIn(value['scope_authorizations'][0]['prior_input']['quote'], prepared['initial_message'])

    def test_pause_between_planning_and_binding_prevents_implementation(self):
        from factory.planning_binding import adopt
        self.start()
        def paused(store, run):
            self.service.pause(self.pid)
            return adopt(store, run)
        with patch('factory.planning_binding.adopt', paused), self.assertRaises(FactoryError) as error:
            self.drive()
        self.assertEqual(error.exception.code, 'paused')
        self.assertEqual(self.worker.contexts, [])
        self.service.resume(self.pid); self.drive(); self.assert_finished()

    def test_prior_scope_authorization_is_preserved_without_synthetic_answer(self):
        key = 'out_of_scope'
        next(c for c in self.plan['coverage'] if c['requirement'] == key).update(
            disposition='out_of_scope', slices=[], rationale='External services explicitly excluded in the initial brief')
        for s in self.plan['slices']:
            s['requirements'].remove(key)
        self.plan['milestones'][1]['requirements'].remove(key)
        self.binding['requirements'] = [r for r in self.binding['requirements'] if r['requirement'] != key]
        self.definition['scope_authorizations'] = [{'id':'no_services','disposition':'out_of_scope',
            'prior_input':{'request_id':'initial-brief','quote':'External services excluded.'}}]
        self.binding['exclusions'] = [{'requirement':key,'authorization':'no_services'}]
        self.start(); self.drive(); self.assert_finished()
        self.assertEqual(self.store.snapshot()['decisions'], [])
        excluded = ProjectStore(self.store).receipts()[0]['exclusions'][0]
        self.assertEqual(excluded['authorization']['prior_input']['request_id'], 'initial-brief')

    def test_binding_cannot_replace_checks_hide_transversal_gaps_or_add_resources(self):
        self.start()
        bound = context(self.store)
        p = deepcopy(self.plan)
        p['execution_binding'] = {**deepcopy(self.binding), 'definition_id':bound['definition_id']}
        # The compiler consumes the same authoritative source shape supplied by planning.
        from factory.planning import source_snapshot
        source = source_snapshot(self.reference_store.snapshot())
        def compile(value):
            return compile_binding(value, source, bound['templates'], bound['definition_id'], self.policy)
        self.assertEqual(len(compile(p)['checks']), len(self.definition['checks']))
        for change in ('weaken', 'replace', 'transversal', 'resource', 'stale', 'browser'):
            bad = deepcopy(p); mapping = bad['execution_binding']
            if change == 'weaken': mapping['checks'][0]['min_tests'] = 0
            if change == 'replace': mapping['checks'][0]['template'] = 'tests'
            if change == 'transversal': mapping['requirements'][0]['checks'] = ['integrated','description_close']
            if change == 'resource': mapping['harness'][0]['paths'].append('unapproved.py')
            if change == 'stale': mapping['definition_id'] = 'obsolete'
            if change == 'browser': bad['gates'][-1]['checks'][0] = 'Run mandatory browser validation'
            with self.subTest(change=change), self.assertRaises(FactoryError): compile(bad)
