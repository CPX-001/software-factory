"""Planning fixtures: no SDK, filesystem skill discovery or quota."""
from copy import deepcopy
from factory.planning import Planning, source_snapshot
from factory.skill_catalog import Catalog
from factory.skill_router import SkillRouter
from tests.architecture_fakes import FakeArchitect, review


def proposal(source):
    binding = source['architecture']
    keys = [r['key'] for r in source['requirements']]
    return {'schema_version': 1, 'architecture': deepcopy(binding),
        'objective': 'Deliver a coherent verifiable user workflow.',
        'milestones': [{'id': 'm1', 'title': 'Primary workflow usable',
            'objective': 'User can complete the main workflow and observe the result.',
            'requirements': list(keys), 'success_criteria': ['Principal user completes the workflow successfully'],
            'dependencies': [], 'risks': [], 'observable_outcome': 'Usable product result',
            'closure_conditions': ['All scoped acceptance criteria verified'],
            'architecture': deepcopy(binding), 'verification_gates': ['milestone_gate'], 'status': 'open'}],
        'slices': [{'id': 's1', 'milestone': 'm1', 'title': 'First usable outcome',
            'objective': 'Accept user input, apply policy and return an observable result.',
            'kind': 'vertical', 'requirements': list(keys), 'dependencies': [],
            'scope': ['Complete the primary happy path'], 'out_of_scope': ['Advanced variants'],
            'acceptance_criteria': ['Valid input produces the expected observable result'],
            'components': ['app'], 'boundaries': [], 'risks': [], 'architecture_impact': 'none',
            'impact_reason': '', 'verification_expectation': 'Focused behavior test with fixture',
            'verification_triggers': [], 'maturity': 'execution_ready', 'architecture': deepcopy(binding)}],
        'near_term': ['s1'], 'risks': [],
        'coverage': [{'requirement': key, 'disposition': 'covered', 'milestone': 'm1', 'slices': ['s1'],
                      'rationale': 'Primary workflow and milestone criteria own this requirement'} for key in keys],
        'gates': [{'id': 'local_gate', 'kind': 'local', 'trigger': 'after_slice', 'target': 's1',
            'checks': ['Focused behavior test for primary outcome'], 'harness': ['unit'], 'cost': 'cheap',
            'rationale': 'Cheap check for the changed behavior', 'signals': ['routine']},
            {'id': 'milestone_gate', 'kind': 'milestone', 'trigger': 'milestone_close', 'target': 'm1',
            'checks': ['Demonstrate principal workflow and all success criteria'], 'harness': ['unit'],
            'cost': 'moderate', 'rationale': 'Verify the meaningful product capability', 'signals': ['milestone']}],
        'harness': [{'id': 'unit', 'capability': 'Unit framework with representative fixtures',
            'why': 'Validate outcomes cheaply', 'introduced_by': 's1', 'milestone': 'm1',
            'when': 'during_slice', 'needed_by_gates': ['local_gate', 'milestone_gate']}],
        'unresolved_questions': [], 'decision_keys': [], 'blockers': [],
        'rationale': 'One coherent initial capability with inexpensive local validation.'}


def dynamic(context):
    p = proposal(context['source'])
    p['decision_keys'] = [d['key'] for d in context['human_answers'] if d.get('origin') == 'planning' and d.get('answer')]
    return p


def fake():
    return FakeArchitect(dynamic, review())


def finish(store):
    return Planning(store, fake(), SkillRouter(Catalog())).run()
