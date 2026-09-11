"""Extend the two-milestone scenario through a real Python CLI and final acceptance."""
from copy import deepcopy

from factory.execution_store import ExecutionStore
from tests.milestone_fakes import setup_milestones
from tests.continuation_fakes import implementation_a, implementation_b, implementation_c


def cli_implementation(*, broken=False):
    value = implementation_c()
    value['changes'][0]['content'] += '\nif __name__ == "__main__":\n    import sys\n    print(describe(int(sys.argv[1]), int(sys.argv[2])))\n'
    if broken:
        value['changes'][0]['content'] = value['changes'][0]['content'].replace('int(sys.argv[1]), int(sys.argv[2])', 'int(sys.argv[1]), 0')
    return value


def final_repair(context):
    value = cli_implementation()
    value['criteria_addressed'] = list(range(len(context['slice']['acceptance_criteria'])))
    return value


def setup_project(root, *, broken=True, authorized=True, remediation=True, custom=None, customize_definition=None, authorize_exclusions=False):
    def plan(p):
        gate = deepcopy(next(g for g in p['gates'] if g['id'] == 'close_m2'))
        gate.update(id='project_entry', kind='system', trigger='project_close', target='m2',
                    checks=['The real CLI composes signed inputs and prints the accepted description'],
                    rationale='Final integrated user journey after both milestone capabilities are available',
                    signals=['release'])
        p['gates'].append(gate)
        if custom:
            custom(p)
    service, store, jobs, git, sdk, refiner = setup_milestones(root, limit=4, custom=plan)
    (store.project / 'test_entry.py').write_text('''import unittest
from final import describe
class EntryContract(unittest.TestCase):
    def test_signed_description(self):
        self.assertEqual(describe(-1, 2), "sum=2")
''')
    (store.project / 'USAGE.md').write_text('Run: /usr/bin/python3 -S final.py -1 2\nExpected: sum=2\nPython standard library only.\n')
    git('add', 'test_entry.py', 'USAGE.md'); git('commit', '-qm', 'Approved final CLI acceptance and usage')
    journal = ExecutionStore(store)
    policy = {k: v for k, v in journal.policy().items() if k not in ('repository', 'definition_id', 'authorized_at')}
    policy.update(final_validation={'enabled': authorized, 'automatic_remediation': remediation})
    policy['context_paths'] += ['test_entry.py', 'USAGE.md']
    definition = journal.definition(journal.policy()['definition_id'])['verification']
    check = deepcopy(next(c for c in definition['checks'] if c['id'] == 'integrated'))
    check.update(id='entry', gate='project_entry', target='test_entry.py', criteria=[],
                 entrypoint={'path': 'final.py', 'args': ['-1', '2'], 'stdout': 'sum=2\n'})
    definition['checks'].append(check)
    definition['requirement_acceptance'] = [{'requirement': c['requirement'],
        'condition': next(r['text'] for r in store.snapshot()['planning']['roadmap']['source']['requirements'] if r['key'] == c['requirement']),
        'milestones': ['m1', 'm2'], 'checks': ['integrated', 'description_close', 'entry']}
        for c in store.snapshot()['planning']['roadmap']['plan']['coverage'] if c['disposition'] == 'covered']
    definition['project_acceptance'] = {'entry_checks': ['entry'], 'delivery_paths': ['USAGE.md'],
                                       'runtime': 'python_stdlib', 'exclusions': []}
    if authorize_exclusions:
        for coverage in store.snapshot()['planning']['roadmap']['plan']['coverage']:
            if coverage['disposition'] in ('out_of_scope', 'deferred'):
                decision = store.request_decision('Accept ' + coverage['requirement'] + ' as ' + coverage['disposition'] + ': ' + coverage['rationale'],
                                                  store.snapshot()['revision'])
                store.answer_decision(decision, 'accept', store.snapshot()['revision'])
                definition['project_acceptance']['exclusions'].append({'requirement': coverage['requirement'], 'decision_id': decision})
    if customize_definition:
        customize_definition(policy, definition)
    service.configure_execution(policy, definition)
    sdk.responses = [implementation_a(), implementation_b(), cli_implementation(broken=broken), final_repair]
    return service, store, jobs, git, sdk, refiner
