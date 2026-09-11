"""Two dependent milestones with a real integration regression introduced by B."""
from copy import deepcopy

from tests.continuation_fakes import setup, implementation_a, implementation_b, implementation_c, refinement
from factory.execution_store import ExecutionStore


def broken_b():
    value = implementation_b()
    value['changes'].append({'path': 'app.py', 'operation': 'write',
        'content': 'def add(a, b):\n    return max(0, a) + b\n'})
    return value


def remediation():
    value = implementation_a()
    value['criteria_addressed'] = [0, 1]
    return value


def setup_milestones(root, *, inter=True, limit=4, custom=None, empty_outline=False):
    def plan(p):
        first = p['milestones'][0]
        first.update(success_criteria=['Composed sum works for signed inputs'],
                     closure_conditions=['Accepted addition and composition contracts remain compatible'])
        second = deepcopy(first)
        second.update(id='m2', title='Describe the accepted result', objective='Format the accepted composition result',
            dependencies=['m1'], verification_gates=['close_m2'],
            success_criteria=['Description includes the accepted composed sum'],
            closure_conditions=['Description passes the approved fixed behavior case'])
        p['milestones'].append(second)
        p['slices'][2]['milestone'] = 'm2'
        if empty_outline:
            p['slices'][2].update(scope=[], acceptance_criteria=[], components=[], boundaries=[])
        p['slices'][1]['maturity'] = 'execution_ready'
        p['near_term'] = ['s1', 's2']
        gate = deepcopy(next(g for g in p['gates'] if g['kind'] == 'milestone'))
        gate.update(id='close_m2', target='m2', harness=[])
        p['gates'].append(gate)
        if custom:
            custom(p)
    service, store, jobs, git, sdk, refiner = setup(root, custom=plan)
    (store.project / 'test_integrated.py').write_text('''import unittest
from app import add
from report import double_sum
class Integration(unittest.TestCase):
    def test_signed_contract(self):
        self.assertEqual(add(-1, 2), 1)
        self.assertEqual(double_sum(-1, 2), 2)
''')
    git('add', 'test_integrated.py'); git('commit', '-qm', 'Approved milestone integration oracle')
    journal = ExecutionStore(store)
    policy = {k: v for k, v in journal.policy().items() if k not in ('repository', 'definition_id', 'authorized_at')}
    policy['context_paths'].append('test_integrated.py')
    policy['continuation'].update(inter_milestone=inter, max_slices=limit)
    definition = journal.definition(journal.policy()['definition_id'])['verification']
    check = deepcopy(definition['checks'][1])
    check.update(id='integrated', gate='milestone_gate', target='test_integrated.py', criteria=[0, 1])
    definition['checks'].append(check)
    check = deepcopy(definition['checks'][0])
    check.update(id='description_close', gate='close_m2', target='final:describe', criteria=[0, 1],
        cases=[{'args_json': '[3,4]', 'expected_json': '"sum=14"'}])
    definition['checks'].append(check)
    service.configure_execution(policy, definition)
    sdk.responses = [implementation_a(), broken_b(), remediation(), implementation_c()]
    refiner.responses = [refinement]
    return service, store, jobs, git, sdk, refiner
