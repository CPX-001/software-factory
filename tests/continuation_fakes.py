"""Three dependent slices; real Python imports prove accepted-code continuity."""
from copy import deepcopy

from factory.continuation_store import ContinuationStore
from factory.execution_store import ExecutionStore
from tests.execution_fakes import FakeSDK, fixture, result


def implementation_a(*, harness=False):
    value = result(tests=harness)
    value['changes'][0]['content'] = 'def add(a, b):\n    return a + b\n'
    return value


def implementation_b(wrong=False):
    value = result()
    value['changes'] = [{'path': 'report.py', 'operation': 'write', 'content':
        'from app import add\ndef double_sum(a, b):\n    return ' + ('0' if wrong else '2 * add(a, b)') + '\n'}]
    return value


def implementation_c():
    value = result()
    value['changes'] = [{'path': 'final.py', 'operation': 'write', 'content':
        'from report import double_sum\ndef describe(a, b):\n    return "sum=" + str(double_sum(a, b))\n'}]
    return value


def refinement(context):
    sid = context.get('slice', {}).get('id', context.get('slice_id'))
    return {'slice_id': sid, 'steps': ['Reuse the accepted dependency API in a focused pure Python module'],
        'files': ['report.py' if sid == 's2' else 'final.py'], 'components': ['app'], 'boundaries': [],
        'criteria_checks': [{'criterion': 0, 'checks': ['behavior_' + sid]}], 'risks': [], 'questions': [],
        'proposal': {'kind': 'none', 'reason': '', 'references': []}}


def setup(root, *, ready=False, harness=False, count=3, custom=None):
    def plan(p):
        for i in range(2, count+1):
            s = deepcopy(p['slices'][0]); sid = 's' + str(i)
            s.update(id=sid, dependencies=['s' + str(i-1)], maturity='execution_ready' if ready else 'outline',
                     title='Use the accepted previous result', objective='Compose the accepted dependency result')
            p['slices'].append(s)
            g = deepcopy(p['gates'][0]); g.update(id='gate_' + sid, target=sid)
            p['gates'].append(g); p['harness'][0]['needed_by_gates'].append(g['id'])
            for c in p['coverage']:
                c['slices'].append(sid)
        if ready:
            p['near_term'] = [s['id'] for s in p['slices']]
        if custom:
            custom(p)
    sdk = FakeSDK(implementation_a(harness=harness), implementation_b(), implementation_c())
    service, store, jobs, git = fixture(root, sdk, harness_during=harness, customize=plan)
    (store.project / 'test_report.py').write_text('import unittest\nfrom report import double_sum\nclass Test(unittest.TestCase):\n    def test_dependency(self):\n        self.assertEqual(double_sum(3, 4), 14)\n')
    (store.project / 'test_final.py').write_text('import unittest\nfrom final import describe\nclass Test(unittest.TestCase):\n    def test_chain(self):\n        self.assertEqual(describe(3, 4), "sum=14")\n')
    git('add', 'test_report.py', 'test_final.py'); git('commit', '-qm', 'Acceptance before implementation')
    journal = ExecutionStore(store)
    policy = {k: v for k, v in journal.policy().items() if k not in ('repository', 'definition_id', 'authorized_at')}
    policy.update(write_paths=['app.py', 'report.py', 'final.py', 'test_app.py'],
                  context_paths=['app.py', 'report.py', 'final.py', 'test_app.py', 'test_report.py', 'test_final.py'],
                  continuation={'enabled': True, 'max_slices': 3, 'max_calls': 10, 'max_seconds': 600, 'max_tokens': 50000})
    definition = journal.definition(journal.policy()['definition_id'])['verification']
    for sid, target, expected, testfile in [('s2', 'report:double_sum', '14', 'test_report.py'), ('s3', 'final:describe', '"sum=14"', 'test_final.py')][:count-1]:
        definition['checks'].extend([
            {'id': 'behavior_' + sid, 'gate': 'gate_' + sid, 'kind': 'python_behavior', 'target': target,
             'cases': [{'args_json': '[3,4]', 'expected_json': expected}], 'min_tests': 1, 'timeout_seconds': 5, 'criteria': [0], 'gate_checks': [0]},
            {'id': 'tests_' + sid, 'gate': 'gate_' + sid, 'kind': 'python_unittest', 'target': testfile,
             'cases': [], 'min_tests': 1, 'timeout_seconds': 5, 'criteria': [0], 'gate_checks': [0]}])
    service.configure_execution(policy, definition)
    refiner = FakeSDK(refinement, refinement)
    service.refinement_worker_factory = refiner
    return service, store, jobs, git, sdk, refiner
