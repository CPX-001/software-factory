"""Prepared acceptance scenario. Phase inputs are synthetic; solution is never supplied.

Only creates a NEW disposable repository under the supplied empty temporary root.
Runs discovery/architecture/planning validation and revision publication normally.
"""
from pathlib import Path
from copy import deepcopy

from factory.application import FactoryService
from factory.architecture import Architecture
from factory.discovery import Discovery
from factory.execution_contract import DEFAULT_POLICY
from factory.execution_workspace import git
from factory.planning import Planning, source_snapshot
from factory.registry import Registry
from factory.skill_catalog import Catalog
from factory.skill_router import SkillRouter
from factory.workflow import Store
from tests.architecture_fakes import FakeArchitect, proposal as architecture_proposal, review
from tests.discovery_fakes import FakeModel, complete_reply
from tests.planning_fakes import proposal as planning_proposal

INITIAL_CODE = '''from record_rules import normalized_category


def summarize(records):
    """Return the count and total for a list of records (amount, category)."""
    return {"count": len(records), "total": sum(row["amount"] for row in records)}
'''
EXISTING_HELPER = '''def normalized_category(value):
    """Canonical record category; callers must preserve this established behavior."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("category must be a nonempty string")
    return value.strip().casefold()
'''
ACCEPTANCE = '''import copy
import unittest
from records import summarize


class Acceptance(unittest.TestCase):
    def test_aggregate_existing_records(self):
        self.assertEqual(summarize([
            {"category": " Food ", "amount": 10}, {"category": "FOOD", "amount": 2.5},
            {"category": "Books", "amount": 7}]),
            {"count": 3, "total": 19.5, "by_category": {
                "food": {"count": 2, "total": 12.5}, "books": {"count": 1, "total": 7}}})

    def test_empty(self):
        self.assertEqual(summarize([]), {"count": 0, "total": 0, "by_category": {}})

    def test_normalization_and_zero(self):
        self.assertEqual(summarize([{"category": "Straße", "amount": 0},
                                   {"category": " STRASSE ", "amount": 2}]),
                         {"count": 2, "total": 2, "by_category": {"strasse": {"count": 2, "total": 2}}})

    def test_no_mutation(self):
        data = [{"category": " x ", "amount": 1, "memo": ["keep"]}]
        original = copy.deepcopy(data)
        summarize(data)
        self.assertEqual(data, original)

    def test_reject_non_list(self):
        for value in (None, {}, "rows", ()):
            with self.subTest(value=value), self.assertRaises(ValueError):
                summarize(value)

    def test_reject_non_record(self):
        for row in (None, [], 1, "row"):
            with self.subTest(row=row), self.assertRaises(ValueError):
                summarize([row])

    def test_reject_missing_fields(self):
        for row in ({}, {"category": "x"}, {"amount": 1}):
            with self.subTest(row=row), self.assertRaises(ValueError):
                summarize([row])

    def test_reject_bad_amount(self):
        for amount in (-1, True, False, "1", None, float("nan"), float("inf"), float("-inf")):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                summarize([{"category": "x", "amount": amount}])

    def test_reject_bad_category(self):
        for category in ("", "  ", None, 7, True):
            with self.subTest(category=category), self.assertRaises(ValueError):
                summarize([{"category": category, "amount": 1}])

    def test_valid_then_invalid(self):
        with self.assertRaises(ValueError):
            summarize([{"category": "x", "amount": 1}, {"category": "x", "amount": -2}])
'''


RANKING_ACCEPTANCE = '''import unittest
from category_report import rank_categories

class RankingAcceptance(unittest.TestCase):
    def test_rank_and_normalize(self):
        self.assertEqual(rank_categories([
            {"category": "Books", "amount": 7}, {"category": " Food ", "amount": 10},
            {"category": "FOOD", "amount": 2}]),
            [{"category": "food", "count": 2, "total": 12},
             {"category": "books", "count": 1, "total": 7}])

    def test_equal_total_uses_category_order(self):
        self.assertEqual(rank_categories([{"category": "z", "amount": 1}, {"category": "A", "amount": 1}]),
                         [{"category": "a", "count": 1, "total": 1}, {"category": "z", "count": 1, "total": 1}])

    def test_empty(self):
        self.assertEqual(rank_categories([]), [])

    def test_reuses_validation(self):
        with self.assertRaises(ValueError):
            rank_categories([{"category": "x", "amount": -1}])
'''


def prepare_from_discovery(root, model, effort, *, registry_home=None, automatic_binding=False):
    """A NEW persistent instance of the records scenario, with no generated phase input.

    Only normal project initialization/pause are used. No model, enqueue, acceptance,
    architecture, planning, verification mapping or workflow database write occurs here.
    """
    import hashlib
    import json
    from factory.reproducibility import atomic_json
    from factory.registry import FactoryError
    repo = Path(__file__).resolve().parent.parent
    root = Path(root).expanduser().resolve()
    if root.is_relative_to(repo):
        raise FactoryError('pilot_separate_repository_required', 'The product pilot must be outside the Factory checkout')
    if root.exists():
        raise FactoryError('pilot_exists', 'Creation never overwrites an existing pilot; use --prepared to inspect/resume it')
    root.mkdir(parents=True)
    product = root / 'product'; product.mkdir()
    resources = {
        'record_rules.py': EXISTING_HELPER,
        'test_records.py': ACCEPTANCE,
        'test_category_report.py': RANKING_ACCEPTANCE,
        'test_product_cli.py': (repo / 'pilots/records-v1/test_product_cli.py').read_text(),
        'PILOT.md': (repo / 'pilots/records-v1/brief.md').read_text(),
        '.gitignore': '.factory/\n__pycache__/\n*.pyc\n',
    }
    if automatic_binding:
        for name in ('test_summary_integration.py', 'test_report_integration.py', 'test_delivery_contract.py',
                     'test_no_residual_storage.py'):
            resources[name] = (repo / 'pilots/records-v1' / name).read_text()
    valid = [{'category': 'Books', 'amount': 7}, {'category': ' Food ', 'amount': 10},
             {'category': 'FOOD', 'amount': 2}]
    examples = {'valid.json': valid, 'reordered.json': list(reversed(valid)), 'empty.json': [],
                'negative.json': [{'category': 'x', 'amount': -1}],
                'boolean.json': [{'category': 'x', 'amount': True}]}
    resources.update({'examples/' + name: json.dumps(value, ensure_ascii=False, indent=2) + '\n'
                      for name, value in examples.items()})
    resources['examples/malformed.json'] = '[broken JSON\n'
    for name, content in resources.items():
        target = product / name; target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    checks = [{'id': key, 'gate': 'independent_acceptance', 'kind': 'python_unittest', 'target': path,
               'cases': [], 'min_tests': minimum, 'timeout_seconds': 20, 'criteria': [], 'gate_checks': [0],
               'integration_mode': 'local'}
              for key, path, minimum in [('summary', 'test_records.py', 10),
                  ('ranking', 'test_category_report.py', 4), ('cli', 'test_product_cli.py', 5)]]
    checks[-1]['entrypoint'] = {'path': 'category_report.py', 'args': ['examples/valid.json'],
        'stdout': '[{"category":"food","count":2,"total":12},{"category":"books","count":1,"total":7}]\n'}
    if automatic_binding:
        checks.extend({**deepcopy(checks[0]), 'id': key, 'target': path, 'min_tests': 2}
            for key, path in (('summary_integration', 'test_summary_integration.py'),
                              ('report_integration', 'test_report_integration.py'),
                              ('delivery_contract', 'test_delivery_contract.py')))
        checks.append({**deepcopy(checks[0]), 'id':'no_residual_storage',
                       'target':'test_no_residual_storage.py', 'min_tests':1})
        for check in checks:
            check.update(clean_copy=True, source_sha256=hashlib.sha256(resources[check['target']].encode()).hexdigest())
    contract = {'id': 'records-v1', 'input_kind': 'pilot_test_data', 'independent_of_implementation_worker': True,
        'factory_source': git(repo, 'rev-parse', 'HEAD'),
        'resource_hashes': {p: hashlib.sha256(t.encode()).hexdigest() for p, t in resources.items()},
        'checks': checks, 'required_milestones': 2,
        'milestone_outcomes': ['Reusable validated category summary', 'User-facing deterministic ranking through the accepted summary'],
        'delivery_paths': ['records.py', 'category_report.py', 'USAGE.md'],
        'runtime': 'python_stdlib', 'plan_binding': 'pending real discovery/architecture/planning; these are independent oracle templates, not approved gate mappings'}
    atomic_json(product / 'pilot-contract.json', contract)
    git(product, 'init', '-q')
    git(product, 'add', '.')
    git(product, '-c', 'user.name=Pilot fixture', '-c', 'user.email=fixture@localhost',
        'commit', '-qm', 'Independent records pilot inputs and acceptance, before discovery')
    service = FactoryService(Registry(registry_home))
    service.authorize_root(root)
    status = service.initialize_project(str(product), 'Records end-to-end pilot')
    service.pause(status['project']['id'])  # Preparation never permits an unguarded analysis call.
    policy = {**DEFAULT_POLICY, 'enabled': True, 'model': model, 'effort': effort,
        'max_attempts': 2, 'max_seconds': 180, 'max_tokens': 12000,
        'write_paths': ['records.py', 'category_report.py', 'USAGE.md'],
        'context_paths': list(resources) + ['records.py', 'category_report.py', 'pilot-contract.json'],
        'continuation': {'enabled': True, 'inter_milestone': True, 'max_slices': 2,
                         'max_calls': 4, 'max_seconds': 300, 'max_tokens': 30000},
        'final_validation': {'enabled': True, 'automatic_remediation': False}}
    prepared = {'root': str(root), 'project': status['project'], 'registry_home': str(service.registry.home),
        'baseline_commit': git(product, 'rev-parse', 'HEAD'), 'phase_inputs': 'discovery_brief',
        'scenario': 'records-v1', 'initial_message': resources['PILOT.md'],
        'contract': contract, 'contract_sha256': hashlib.sha256((product / 'pilot-contract.json').read_bytes()).hexdigest(),
        'policy': policy, 'policy_applied': False,
        'full_workflow_limits': {k: policy['continuation'][k] for k in ('max_calls', 'max_seconds', 'max_tokens')},
        'full_workflow_limits_enforced': False,
        'request_id': 'records-v1-discovery-once', 'real_model_requested': False,
        'recovery': 'Use this report with --prepared. Do not recreate or overwrite this directory.'}
    if automatic_binding:
        # Proposal only: the normal policy tool still requires execution authorization.
        policy['automatic_plan_binding'] = True
        prepared['verification_templates'] = {'schema_version': 1, 'checks': checks, 'harness': [],
            'resources': [{'path': p, 'sha256': h} for p, h in contract['resource_hashes'].items()] +
                [{'path': 'pilot-contract.json', 'sha256': prepared['contract_sha256']}],
            'project_acceptance': {'entry_checks': ['cli'], 'delivery_paths': contract['delivery_paths'],
                                   'runtime': 'python_stdlib', 'exclusions': []},
            'scope_authorizations': [{'id': 'initial_exclusions', 'disposition': 'out_of_scope',
                'prior_input': {'request_id': prepared['request_id'], 'quote':
                    'No quiero UI, red, dependencias de terceros, instalación de paquetes, persistencia,\n'
                    'otros lenguajes, despliegue, publicación ni cambios automáticos de arquitectura.'}}]}
    atomic_json(root / 'report.json', prepared)
    return prepared


def bind_discovery_plan(prepared, snapshot):
    """Reviewed binding for the saved REAL pilot plan, not a fabricated replacement plan.

    A different roadmap needs its own reviewed binding. Unknown criteria fail closed;
    no fuzzy text matching or LLM can quietly reinterpret a condition here.
    """
    import hashlib
    import json
    from factory.execution import current_sources
    from factory.registry import FactoryError
    from factory.execution_contract import validate_verification
    from factory.verification import check_coverage
    resources = Path(__file__).resolve().parent.parent / 'pilots/records-v1'
    reviewed = json.loads((resources / 'accepted-plan.json').read_text())
    if current_sources(snapshot) != reviewed['sources'] or snapshot['planning']['roadmap']['plan'] != reviewed['plan']:
        raise FactoryError('verification_binding_pending', 'This reviewed binding belongs to a different accepted roadmap')
    product = Path(prepared['project']['path'])
    if hashlib.sha256((product / 'pilot-contract.json').read_bytes()).hexdigest() != prepared['contract_sha256']:
        raise FactoryError('verification_weakened', 'Original pilot contract changed')
    templates = {c['id']: deepcopy(c) for c in prepared['contract']['checks']}
    for key, path in [('summary_composition', 'test_summary_integration.py'),
                      ('report_composition', 'test_report_integration.py'), ('delivery', 'test_delivery_contract.py')]:
        templates[key] = dict(id=key, gate='pending', kind='python_unittest', target=path, cases=[],
            min_tests=2, timeout_seconds=30, criteria=[], gate_checks=[0], integration_mode='local')
    for c in templates.values():
        c.update(source_sha256=hashlib.sha256((product / c['target']).read_bytes()).hexdigest(), clean_copy=True)
    checks = []
    def add(key, gate, criteria, gate_checks, *, identifier=None):
        checks.append({**deepcopy(templates[key]), 'id': identifier or gate + '_' + key,
                       'gate': gate, 'criteria': criteria, 'gate_checks': gate_checks})
    for gate in ('g_s_summary_local', 'g_s_summary_api'):
        add('summary', gate, [0, 1], [0, 1])
        add('summary_composition', gate, [2], [1])
    add('summary', 'g_m_summary', [0, 1, 2, 4], [0, 1], identifier='summary')
    add('summary_composition', 'g_m_summary', [0, 1, 3, 4], [1])
    add('ranking', 'g_s_rank_local', [0, 3], [0], identifier='ranking')
    add('cli', 'g_s_rank_local', [1, 2, 3], [0, 1])
    add('report_composition', 'g_s_rank_integration', [0], [0])
    add('ranking', 'g_s_rank_integration', [0, 3], [0])
    add('cli', 'g_s_rank_integration', [1, 2, 3], [1])
    for gate in ('g_s_acceptance_local', 'g_s_acceptance_integration'):
        for key in ('summary', 'ranking', 'cli'):
            add(key, gate, [0], [1] if gate.endswith('local') else [0, 1] if key == 'cli' else [0])
        add('delivery', gate, [1, 2], [0, 1] if gate.endswith('local') else [2])
    for gate in ('g_m_report', 'g_project_close'):
        add('summary', gate, [4, 5], [0, 1] if gate == 'g_m_report' else [2])
        add('ranking', gate, [0, 4, 5], [0, 1] if gate == 'g_m_report' else [2])
        add('cli', gate, [1, 2, 4, 5], [0, 1] if gate == 'g_m_report' else [0, 1, 2],
            identifier='cli' if gate == 'g_project_close' else None)
        add('report_composition', gate, [0], [0] if gate == 'g_m_report' else [2])
        add('delivery', gate, [3, 4, 5], [0, 2] if gate == 'g_m_report' else [1, 2])
    plan = reviewed['plan']
    strategic = [c['id'] for c in checks if c['gate'] in ('g_m_summary', 'g_m_report', 'g_project_close')]
    requirements = {r['key']: r['text'] for r in reviewed['requirements']}
    definition = {'schema_version': 1, 'checks': checks,
        'harness': [{'id': 'h_clean_copy_acceptance',
                     'paths': sorted({c['target'] for c in checks} | {'pilot-contract.json', 'record_rules.py'})}],
        'requirement_acceptance': [{'requirement': c['requirement'], 'condition': requirements[c['requirement']],
            'milestones': [m['id'] for m in plan['milestones']], 'checks': strategic}
            for c in plan['coverage'] if c['disposition'] == 'covered'],
        'project_acceptance': {'entry_checks': ['cli'], 'delivery_paths': prepared['contract']['delivery_paths'],
            'runtime': 'python_stdlib', 'exclusions': [
                {'requirement': 'out_of_scope', 'prior_input': {'request_id': prepared['request_id'],
                    'quote': 'No quiero UI, red, dependencias de terceros, instalación de paquetes, persistencia,\notros lenguajes, despliegue, publicación ni cambios automáticos de arquitectura.'}},
                {'requirement': 'quota_planning', 'prior_input': {'request_id': 'records-v1-user-authorize-real-1',
                    'quote': 'el tema de cuotas ya se analizara despues'}}]}}
    validate_verification(definition, plan)
    errors = [e for slice_ in plan['slices'] for e in check_coverage(plan, slice_, definition)]
    if errors:
        raise FactoryError('verification_definition_missing', 'Reviewed binding is incomplete', details={'errors': errors})
    return definition


def prepare(root, model, effort, *, continuation=False):
    root = Path(root)
    product = root / 'product'; product.mkdir()
    (product / 'records.py').write_text(INITIAL_CODE)
    (product / 'record_rules.py').write_text(EXISTING_HELPER)
    (product / 'test_records.py').write_text(ACCEPTANCE)
    if continuation:
        (product / 'test_category_report.py').write_text(RANKING_ACCEPTANCE)
    git(product, 'init', '-q')
    git(product, 'add', 'records.py', 'record_rules.py', 'test_records.py')
    if continuation:
        git(product, 'add', 'test_category_report.py')
    git(product, '-c', 'user.name=Smoke fixture', '-c', 'user.email=fixture@localhost',
        'commit', '-qm', 'Existing utility and predeclared acceptance tests')
    baseline_commit = git(product, 'rev-parse', 'HEAD')
    store = Store(product); store.initialize()
    facts = {
        'vision': 'Extend an existing local Python records utility with validated category aggregates.',
        'users': 'A developer importing records.summarize from Python.',
        'problem': 'The current count/total implementation lacks validation and category summaries.',
        'capabilities': 'summarize(list) validates records, preserves count/total, adds by_category using record_rules.normalized_category.',
        'scope': 'Change records.py only. Keep record_rules.py and the prepared acceptance tests unchanged.',
        'success': 'All ten predeclared unittest tests pass and the fixed behavior examples match.',
        'out_of_scope': 'Persistence, UI, dependencies, deployment, networking and other languages.',
        'constraints': 'Python standard library and unittest only, pure function, no input mutation.',
        'scale': 'Small in-memory lists, used locally by one developer.',
        'security': 'Only synthetic local records; no credentials, services or external IO.',
        'integrations': 'Use the existing local record_rules.normalized_category helper; no external dependencies.',
    }
    if continuation:
        facts['vision'] += ' Then compose its accepted result into a deterministic category ranking.'
        facts['capabilities'] += ' category_report.rank_categories reuses summarize and orders categories by descending total, then category name.'
        facts['scope'] = 'Slice A changes records.py. Slice B adds category_report.py using A. Preserve helpers and predeclared tests.'
        facts['success'] = 'Ten summary tests, four ranking tests and fixed behavior cases pass on the accepted sequential code.'
    reply = complete_reply()
    for item in reply['knowledge']:
        item.update(text=facts[item['key']], basis='Prepared disposable smoke specification')
    Discovery(store, FakeModel(reply)).submit('Prepared local records smoke; acceptance tests precede implementation.')
    router = SkillRouter(Catalog())
    architecture = architecture_proposal(store.snapshot()['discovery']['knowledge'])
    architecture['runtime'][0]['description'] = 'Local Python standard library module, no external IO.'
    architecture['testing'][0]['description'] = 'Prepared unittest acceptance tests and fixed input/output cases.'
    architecture['invariants'][0]['description'] = 'Preserve input records and existing normalized_category semantics.'
    Architecture(store, FakeArchitect(architecture, review()), router).run()
    plan = planning_proposal(source_snapshot(store.snapshot()))
    plan['objective'] = facts['vision']
    slice_ = plan['slices'][0]
    slice_.update(title='Validated record summary', objective=facts['capabilities'],
        scope=['Extend records.summarize preserving count/total and adding by_category with count/total per canonical category.',
               'Accept only a list of dictionaries with amount and category; allow extra record keys.',
               'amount must be int/float, finite, nonnegative and not bool; category must satisfy the existing helper.',
               'Every invalid input raises ValueError; preserve the caller data. Empty list returns count=0,total=0,by_category={}.'],
        out_of_scope=[facts['out_of_scope']],
        acceptance_criteria=['Correct whole-list and canonical category counts/totals, including empty lists and zero amounts.',
                             'Invalid outer/record/field inputs raise ValueError; valid inputs remain unchanged.'],
        verification_expectation='Factory executes the pre-existing ten unittest tests plus fixed behavior cases.')
    plan['harness'][0].update(capability='Python unittest acceptance tests', when='before_slice')
    plan['gates'][0]['checks'] = ['Run all ten acceptance tests and fixed category summary cases']
    if continuation:
        slice_['objective'] = 'Implement records.summarize validation and category aggregates in records.py.'
        second = deepcopy(slice_)
        second.update(id='s2', title='Rank accepted category summaries', maturity='outline', dependencies=[slice_['id']],
            objective='Add category_report.rank_categories(records) by calling the accepted records.summarize API.',
            scope=['Create category_report.py only; import and use records.summarize.',
                   'Return a list of category/count/total objects sorted by descending total, then canonical category ascending.',
                   'Empty input returns []; preserve validation and no-mutation behavior through summarize.'],
            acceptance_criteria=['Ranking reuses accepted canonical category aggregates and has deterministic descending total/name ordering.',
                                 'Empty input returns [] and invalid input raises ValueError through the existing summarize API.'],
            verification_expectation='Run the four predeclared ranking unittest tests and fixed ranking cases.')
        plan['slices'].append(second)
        gate = deepcopy(plan['gates'][0]); gate.update(id='ranking_gate', target='s2', checks=['Run ranking acceptance tests and fixed cases'])
        plan['gates'].append(gate)
        plan['harness'][0]['needed_by_gates'].append('ranking_gate')
        for item in plan['coverage']:
            item['slices'].append('s2')
    if continuation:
        milestone_plan(plan)
    Planning(store, FakeArchitect(plan, review()), router).run()
    registry = Registry(root / 'registry'); registry.allow_root(root)
    project = registry.register(str(product), 'Disposable records smoke'); registry.select(project['id'])
    policy = {**DEFAULT_POLICY, 'enabled': True, 'model': model, 'effort': effort,
              'max_attempts': 2, 'max_seconds': 180, 'max_tokens': 8000,
              'write_paths': ['records.py'], 'context_paths': ['records.py', 'record_rules.py', 'test_records.py']}
    # Retain the existing default 25% reserve. No changes after observing quota.
    verification = {'schema_version': 1, 'checks': [
        {'id': 'acceptance', 'gate': 'local_gate', 'kind': 'python_unittest', 'target': 'test_records.py',
         'cases': [], 'min_tests': 10, 'timeout_seconds': 5, 'criteria': [0, 1], 'gate_checks': [0]},
        {'id': 'examples', 'gate': 'local_gate', 'kind': 'python_behavior', 'target': 'records:summarize',
         'cases': [{'args_json': '[[]]', 'expected_json': '{"count":0,"total":0,"by_category":{}}'},
                   {'args_json': '[[{"category":" X ","amount":3},{"category":"x","amount":2}]]',
                    'expected_json': '{"count":2,"total":5,"by_category":{"x":{"count":2,"total":5}}}'}],
         'min_tests': 2, 'timeout_seconds': 5, 'criteria': [0], 'gate_checks': [0]}],
        'harness': [{'id': 'unit', 'paths': ['test_records.py', 'record_rules.py']}]}
    if continuation:
        policy.update(max_tokens=12000, write_paths=['records.py', 'category_report.py'],
            context_paths=['records.py', 'record_rules.py', 'test_records.py', 'category_report.py', 'test_category_report.py'],
            continuation={'enabled': True, 'inter_milestone': True, 'max_slices': 2, 'max_calls': 4, 'max_seconds': 300, 'max_tokens': 30000})
        verification['checks'].extend([
            {'id': 'ranking_acceptance', 'gate': 'ranking_gate', 'kind': 'python_unittest', 'target': 'test_category_report.py',
             'cases': [], 'min_tests': 4, 'timeout_seconds': 5, 'criteria': [0, 1], 'gate_checks': [0]},
            {'id': 'ranking_examples', 'gate': 'ranking_gate', 'kind': 'python_behavior', 'target': 'category_report:rank_categories',
             'cases': [{'args_json': '[[{"category":"X","amount":2},{"category":"y","amount":4},{"category":" x ","amount":1}]]',
                        'expected_json': '[{"category":"y","count":1,"total":4},{"category":"x","count":2,"total":3}]'}],
             'min_tests': 1, 'timeout_seconds': 5, 'criteria': [0], 'gate_checks': [0]}])
        milestone_checks(verification)
        final_checks(plan, verification)
        policy['final_validation'] = {'enabled': True, 'automatic_remediation': False}
    return {'root': str(root), 'project': project, 'registry_home': str(registry.home),
            'policy': policy, 'verification': verification, 'baseline_commit': baseline_commit}


def milestone_plan(plan):
    """Adapt the SAME unexecuted records pilot, keeping architecture and slice oracles."""
    if any(m['id'] == 'm2' for m in plan['milestones']):
        return
    first = plan['milestones'][0]
    first.update(title='Validated category aggregates', objective='The summary API validates and aggregates local records',
        success_criteria=['The ten approved summary tests pass on integrated code'],
        closure_conditions=['Fixed summary cases and the existing normalization contract remain valid'])
    second = deepcopy(first)
    second.update(id='m2', title='Rank accepted categories', objective='Compose and rank the accepted summary API',
        dependencies=[first['id']], verification_gates=['ranking_close'],
        success_criteria=['The four approved ranking tests pass on integrated code'],
        closure_conditions=['The ten summary regressions also pass with the composed ranking implementation'])
    plan['milestones'].append(second)
    plan['slices'][1]['milestone'] = 'm2'
    gate = deepcopy(next(g for g in plan['gates'] if g['id'] == 'milestone_gate'))
    gate.update(id='ranking_close', target='m2', harness=[], checks=['Run ranking and summary tests on the integrated candidate'])
    plan['gates'].append(gate)


def milestone_checks(verification):
    for source, identifier, gate, mapped in [('acceptance', 'summary_close', 'milestone_gate', [0, 1]),
            ('examples', 'summary_close_examples', 'milestone_gate', [1]),
            ('ranking_acceptance', 'rank_close', 'ranking_close', [0]),
            ('ranking_examples', 'rank_close_examples', 'ranking_close', [0]),
            ('acceptance', 'summary_regression', 'ranking_close', [1])]:
        if any(c['id'] == identifier for c in verification['checks']):
            continue
        check = deepcopy(next(c for c in verification['checks'] if c['id'] == source))
        check.update(id=identifier, gate=gate, criteria=mapped)
        verification['checks'].append(check)


def final_checks(plan, verification):
    """Bind complete acceptance to existing records/ranking oracles; no new success rule."""
    verification['requirement_acceptance'] = [{'requirement': c['requirement'],
        'condition': 'The approved summary and ranking contracts pass together on the clean accepted candidate.',
        'milestones': ['m1', 'm2'], 'checks': ['summary_close', 'summary_close_examples', 'rank_close', 'rank_close_examples']}
        for c in plan['coverage'] if c['disposition'] == 'covered']
    verification['project_acceptance'] = {'entry_checks': ['rank_close_examples'],
        'delivery_paths': ['records.py', 'record_rules.py', 'category_report.py', 'test_records.py', 'test_category_report.py'],
        'runtime': 'python_stdlib', 'exclusions': []}


def upgrade_prepared(prepared):
    """One-time fixture revision, only before any model run. Not a product replan API."""
    from factory.continuation_store import ContinuationStore
    from factory.execution_store import ExecutionStore
    from factory.planning import append_revision, quality_gate
    from factory.registry import FactoryError
    store = Store(prepared['project']['path']); store.initialize()
    if ExecutionStore(store).latest() or ContinuationStore(store).latest():
        raise FactoryError('smoke_already_started', 'Do not replace sources or reset budgets of an attempted smoke')
    snapshot = store.snapshot()
    plan = deepcopy(snapshot['planning']['roadmap']['plan'])
    if len(plan['milestones']) == 1:
        milestone_plan(plan)
        source = source_snapshot(snapshot)
        gate = quality_gate(plan, source)
        if not gate['passed']:
            raise FactoryError('smoke_plan_invalid', 'Prepared two-milestone fixture failed planning gates', details=gate)
        with store._connection(write=True) as db:
            append_revision(db, plan, source, {'findings': [], 'rationale': 'Synthetic prepared fixture adaptation'}, gate,
                'Step 10: reuse the unexecuted records pilot for two milestone closures',
                expected_parent=snapshot['planning']['roadmap']['revision'])
    prepared = deepcopy(prepared)
    prepared['policy']['continuation']['inter_milestone'] = True
    milestone_checks(prepared['verification'])
    final_checks(plan, prepared['verification'])
    prepared['policy']['final_validation'] = {'enabled': True, 'automatic_remediation': False}
    return prepared
