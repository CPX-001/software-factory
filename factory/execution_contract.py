"""Versioned, deliberately small execution contracts. No shell strings at the API."""
from pathlib import PurePosixPath
import re

from .discovery_contract import KEY, array, enum, obj, string, validate
from .registry import FactoryError

POLICY_SCHEMA = obj({
    'enabled': {'type': 'boolean'}, 'model': string(100), 'effort': string(30),
    'max_attempts': {'type': 'integer', 'minimum': 1, 'maximum': 3},
    'max_seconds': {'type': 'integer', 'minimum': 10, 'maximum': 3600},
    'max_tokens': {'type': 'integer', 'minimum': 1, 'maximum': 500000},
    'quota_reserve_percent': {'type': 'integer', 'minimum': 0, 'maximum': 95},
    'quota_max_age_seconds': {'type': 'integer', 'minimum': 5, 'maximum': 300},
    'quota_bucket': string(100),
    'write_paths': array(string(300), 30), 'context_paths': array(string(300), 30),
})
# Optional, explicit opt-in. Existing policies keep their one-slice authorization.
POLICY_SCHEMA['properties']['continuation'] = obj({
    'enabled': {'type': 'boolean'},
    'max_slices': {'type': 'integer', 'minimum': 1, 'maximum': 50},
    'max_calls': {'type': 'integer', 'minimum': 1, 'maximum': 150},
    'max_seconds': {'type': 'integer', 'minimum': 10, 'maximum': 14400},
    'max_tokens': {'type': 'integer', 'minimum': 1, 'maximum': 2000000},
})
DEFAULT_POLICY = dict(enabled=False, model='', effort='', max_attempts=3, max_seconds=600,
    max_tokens=30000, quota_reserve_percent=25, quota_max_age_seconds=60, quota_bucket='codex',
    write_paths=[], context_paths=[])
POLICY_SCHEMA['properties']['continuation']['properties']['inter_milestone'] = {'type': 'boolean'}
POLICY_SCHEMA['properties']['final_validation'] = obj({
    'enabled': {'type': 'boolean'}, 'automatic_remediation': {'type': 'boolean'},
})

CHECK_SCHEMA = obj({
    'id': KEY, 'gate': KEY, 'kind': enum(('python_behavior', 'python_unittest', 'specialist', 'human_review')),
    'target': string(300), 'cases': array(obj({'args_json': string(4000), 'expected_json': string(4000)}), 100),
    'min_tests': {'type': 'integer', 'minimum': 1, 'maximum': 10000},
    'timeout_seconds': {'type': 'integer', 'minimum': 1, 'maximum': 300},
    'criteria': array({'type': 'integer', 'minimum': 0}, 100),
    'gate_checks': array({'type': 'integer', 'minimum': 0}, 100),
})
CHECK_SCHEMA['properties']['integration_mode'] = enum(('local', 'simulated', 'external_service'))
CHECK_SCHEMA['properties']['entrypoint'] = obj({
    'path': string(300), 'args': array(string(4000, empty=True), 30), 'stdout': string(8000, empty=True),
})
VERIFICATION_SCHEMA = obj({'schema_version': {'type': 'integer', 'enum': [1]},
    'checks': array(CHECK_SCHEMA, 100),
    'harness': array(obj({'id': KEY, 'paths': array(string(300), 30)}), 100)})
VERIFICATION_SCHEMA['properties']['requirement_acceptance'] = array(obj({
    'requirement': KEY, 'condition': string(2000), 'milestones': array(KEY, 100),
    'checks': array(KEY, 100),
}), 150)
VERIFICATION_SCHEMA['properties']['project_acceptance'] = obj({
    'entry_checks': array(KEY, 30),
    'delivery_paths': array(string(300), 30),
    'runtime': enum(('python_stdlib',)),
    'exclusions': array(obj({'requirement': KEY, 'decision_id': {'type': 'integer', 'minimum': 1}}), 150),
})

RESULT_SCHEMA = obj({
    'summary': string(3000),
    'changes': array(obj({'path': string(300), 'operation': enum(('write', 'delete')),
                         'content': string(100000, empty=True)}), 50),
    'criteria_addressed': array({'type': 'integer', 'minimum': 0}, 100),
    'checks_declared': array(string(1000), 20), 'risks': array(string(1500), 10),
    'questions': array(string(2000), 3),
    'architecture': obj({'impact': enum(('none', 'proposal')), 'reason': string(3000, empty=True),
                         'references': array(string(300), 20)}),
    'harness_paths': array(string(300), 30),
    'read_paths': array(string(300), 10),
})

PROTECTED = {'.git', '.factory', '.codex', '.agents', '.env'}


def relative_path(value):
    p = PurePosixPath(value)
    if (not value or p.is_absolute() or '..' in p.parts or '\\' in value or
            any(c in value for c in ('\0', '\n', '\r')) or
            any(part in PROTECTED or part.startswith('.env.') for part in p.parts) or
            p.as_posix() != value or value == '.'):
        raise FactoryError('unsafe_path', 'Path must be a relative product path without protected components')
    return value


def allowed(path, roots):
    relative_path(path)
    return any(path == root or path.startswith(root + '/') for root in roots)


def validate_policy(policy):
    check_schema(policy, POLICY_SCHEMA)
    for path in policy['write_paths'] + policy['context_paths']:
        relative_path(path)
    if policy['enabled'] and not policy['write_paths']:
        raise FactoryError('permissions_missing', 'Authorize explicit product paths before execution')


def validate_result(result):
    validate(result, RESULT_SCHEMA)
    if len(str(result).encode()) > 250000:
        raise FactoryError('invalid_output', 'Worker output exceeds 250 KB')
    paths = [relative_path(c['path']) for c in result['changes']]
    if len(set(paths)) != len(paths):
        raise FactoryError('invalid_output', 'Duplicate change paths')
    for path in result['read_paths'] + result['harness_paths']:
        relative_path(path)


def validate_verification(definition, plan):
    import json
    check_schema(definition, VERIFICATION_SCHEMA)
    gates = {g['id']: g for g in plan['gates']}
    slices = {s['id']: s for s in plan['slices']}
    milestones = {m['id']: m for m in plan['milestones']}
    ids = set()
    for c in definition['checks']:
        if c['id'] in ids or c['gate'] not in gates:
            raise FactoryError('invalid_verification', 'Duplicate check or unknown gate')
        ids.add(c['id'])
        if c.get('entrypoint'):
            relative_path(c['entrypoint']['path'])
            if c['kind'] != 'python_unittest' or not c['entrypoint']['path'].endswith('.py'):
                raise FactoryError('invalid_verification', 'Real product entries use Python scripts alongside unittest checks')
        gate = gates[c['gate']]
        if not c['gate_checks'] or any(i >= len(gate['checks']) for i in c['gate_checks']):
            raise FactoryError('invalid_verification', 'Checks must map to the planned gate criteria')
        if gate['target'] in slices:
            from .verification import outline_criteria
            if any(i >= len(outline_criteria(plan, slices[gate['target']])) for i in c['criteria']):
                raise FactoryError('invalid_verification', 'Unknown slice acceptance criterion')
        if gate['target'] in milestones:
            milestone = milestones[gate['target']]
            if any(i >= len(milestone['success_criteria'] + milestone['closure_conditions']) for i in c['criteria']):
                raise FactoryError('invalid_verification', 'Unknown milestone criterion (success criteria then closure conditions)')
        if c['kind'] == 'human_review' and gate['trigger'] not in ('milestone_close', 'project_checkpoint', 'project_close'):
            raise FactoryError('invalid_verification', 'Human review is supported at milestone closure/checkpoints')
        if c['kind'] == 'python_behavior':
            if not re.fullmatch(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*', c['target']):
                raise FactoryError('invalid_verification', 'Behavior target is module:function')
            if len(c['cases']) < c['min_tests']:
                raise FactoryError('invalid_verification', 'Behavior checks need fixed input/output cases')
            for case in c['cases']:
                try:
                    args = json.loads(case['args_json'])
                    json.loads(case['expected_json'])
                except ValueError as exc:
                    raise FactoryError('invalid_verification', 'Behavior cases must contain valid JSON') from exc
                if not isinstance(args, list):
                    raise FactoryError('invalid_verification', 'Case args must be a JSON array')
        elif c['kind'] == 'python_unittest':
            relative_path(c['target'])
            if not c['target'].endswith('.py') or c['cases']:
                raise FactoryError('invalid_verification', 'Unittest checks select an exact Python test file')
    harness = {h['id'] for h in plan['harness']}
    seen = set()
    for h in definition['harness']:
        if h['id'] not in harness or h['id'] in seen or not h['paths']:
            raise FactoryError('invalid_verification', 'Harness must bind to a planned capability and concrete files')
        seen.add(h['id'])
        for path in h['paths']:
            relative_path(path)
    seen_requirements = set()
    checks = {c['id']: c for c in definition['checks']}
    for contract in definition.get('requirement_acceptance', []):
        key = contract['requirement']
        coverage = next((c for c in plan['coverage'] if c['requirement'] == key), None)
        contributing = {slices[s]['milestone'] for s in coverage['slices']} if coverage else set()
        if (key in seen_requirements or not coverage or coverage['disposition'] != 'covered' or
                not contract['checks'] or not set(contract['milestones']) <= milestones.keys() or
                not contributing <= set(contract['milestones']) or
                any(c not in checks or gates[checks[c]['gate']]['target'] not in contract['milestones']
                    for c in contract['checks'])):
            raise FactoryError('invalid_verification', 'Full requirement acceptance must retain all contributing milestones and strategic checks')
        seen_requirements.add(key)
    project = definition.get('project_acceptance')
    if project:
        for path in project['delivery_paths']:
            relative_path(path)
        if any(c not in checks or checks[c]['kind'] not in ('python_unittest', 'python_behavior') for c in project['entry_checks']):
            raise FactoryError('invalid_verification', 'Product entry checks must reference declared Python procedures')
        if len({e['requirement'] for e in project['exclusions']}) != len(project['exclusions']):
            raise FactoryError('invalid_verification', 'Duplicate project exclusion')


def check_schema(value, schema):
    from jsonschema import validate as check, ValidationError
    try:
        check(value, schema)
    except ValidationError as exc:
        raise FactoryError('invalid_execution_contract', 'Execution input does not match its closed schema',
                           details={'field': '.'.join(map(str, exc.path))}) from exc
