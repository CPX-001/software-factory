"""Architecture fixtures: no SDK, no catalog subprocess, no quota."""
from copy import deepcopy

from factory.architecture_contract import ARCHITECTURE_SCHEMA
from factory.architecture import Architecture
from factory.skill_catalog import Catalog
from factory.skill_router import SkillRouter
from tests.discovery_fakes import complete_reply


def proposal(facts=None):
    facts = facts or complete_reply()['knowledge']
    result = {key: [] for key in ARCHITECTURE_SCHEMA['properties']}
    result.update(schema_version=1, style={'name': 'Modular monolith', 'rationale': 'Keep ownership explicit and operations small.'},
        rationale='A baseline focused on boundaries and verification, with reversible implementation choices.',
        components=[{'id': 'app', 'name': 'Application', 'responsibilities': ['Execute the principal user workflow'],
                     'boundary': 'Owns application policy; adapters isolate external IO.', 'owns': ['Application state']}],
        information_flows=[{'id': 'main_flow', 'steps': ['app'], 'description': 'User input -> validated use case -> result.'}],
        proposed_decisions=[{'id': 'style_choice', 'kind': 'system_style', 'title': 'Application structure',
                             'choice': 'Modular monolith', 'alternatives': ['Separate services'],
                             'rationale': 'Current scale fits one runtime.', 'consequences': ['Keep module ownership enforceable'],
                             'evidence': ['vision'], 'human_key': ''}])
    for name in ('runtime', 'security', 'observability', 'testing', 'invariants', 'constraints'):
        result[name] = [{'id': name + '_policy', 'description': 'Explicit ' + name + ' strategy for the primary application', 'components': ['app']}]
    result['coverage'] = [{'source_key': f['key'], 'targets': ['security_policy' if f['category'] == 'security' else 'app'],
                           'rationale': 'Application responsibility and policy satisfy this fact.'} for f in facts]
    result['assumptions'] = [{'id': 'assumption_' + f['key'], 'statement': f['text'], 'rationale': f['basis'],
                              'validation': 'Validate at the first relevant milestone', 'source_keys': [f['key']]}
                             for f in facts if f['status'] == 'assumption']
    return result


def review(findings=None):
    return {'findings': findings or [], 'rationale': 'Independent bounded review of significant risks and coherence.'}


def finding(category='risk'):
    return {'id': 'capacity_risk', 'severity': 'high', 'category': category,
            'description': 'Runtime capacity may not support peak load.', 'targets': ['runtime_policy'],
            'recommendation': 'Measure peak capacity before launch.'}


def question():
    return {'key': 'isolation_choice', 'question': 'Shared or dedicated isolation?',
            'options': ['Shared database', 'Dedicated database'], 'recommendation': 'Shared with enforced ownership',
            'consequences': ['Shared is cheaper; dedicated provides a stronger isolation boundary.']}


class FakeArchitect:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.contexts = []
        self.inputs = []

    def respond(self, context, *, skill_inputs):
        self.contexts.append(deepcopy(context))
        self.inputs.append(deepcopy(skill_inputs))
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        if callable(result):
            result = result(context)
        return deepcopy(result)


def finish(store):
    facts = store.snapshot()['discovery']['knowledge']
    return Architecture(store, FakeArchitect(proposal(facts), review()), SkillRouter(Catalog())).run()
