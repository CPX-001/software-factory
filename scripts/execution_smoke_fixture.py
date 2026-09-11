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
            ('acceptance', 'summary_regression', 'ranking_close', [1])]:
        if any(c['id'] == identifier for c in verification['checks']):
            continue
        check = deepcopy(next(c for c in verification['checks'] if c['id'] == source))
        check.update(id=identifier, gate=gate, criteria=mapped)
        verification['checks'].append(check)


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
    return prepared
