"""Deterministic execution of immutable checks bound to planning criteria."""
import json
from pathlib import Path
import tempfile
import hashlib
import time
import re
from copy import deepcopy

from .execution_workspace import code_identity, files
from .registry import FactoryError


def gates_for(plan, slice_id, trigger):
    return [g for g in plan['gates'] if g['target'] == slice_id and g['trigger'] == trigger]


def outline_criteria(plan, slice_):
    """An empty outline may inherit ONLY existing approved after-slice obligations."""
    return slice_['acceptance_criteria'] or [text for g in gates_for(plan, slice_['id'], 'after_slice') for text in g['checks']]


def execution_definition(plan, definition):
    definition = deepcopy(definition)
    for slice_ in plan['slices']:
        if slice_['acceptance_criteria']:
            continue
        offset = 0
        for gate in gates_for(plan, slice_['id'], 'after_slice'):
            for check in definition['checks']:
                if check['gate'] == gate['id'] and not check['criteria']:
                    check['criteria'] = [offset + i for i in check['gate_checks']]
            offset += len(gate['checks'])
    return definition


def capability_errors(plan, slice_, definition, policy, baseline):
    """Early capability checks, limited to the selected slice and its required gates.

    Typed checks are authoritative. Named runtime/tool requirements are additional
    conservative signals, not a claim to understand arbitrary natural language.
    Out-of-scope work and future milestone gates do not block this slice.
    """
    gates = gates_for(plan, slice_['id'], 'before_slice') + gates_for(plan, slice_['id'], 'after_slice')
    ids = {g['id'] for g in gates}
    required = {h for g in gates for h in g['harness']}
    errors = ['specialist_review_unsupported:' + c['id'] for c in definition['checks']
              if c['gate'] in ids and c['kind'] == 'specialist']
    errors += ['external_service_unavailable:' + c['id'] for c in definition['checks']
               if c['gate'] in ids and c.get('integration_mode') == 'external_service']
    descriptions = [h['capability'] for h in plan['harness'] if h['id'] in required]
    descriptions += [t for g in gates for t in g['checks']]
    descriptions += slice_['scope'] + [slice_['verification_expectation']]
    descriptions += [e['description'] for section in ('runtime', 'testing') for e in baseline[section]
                     if set(e['components']) & set(slice_['components'])]
    unsupported = {'node': r'\b(node(?:\.js)?|npm|pnpm|yarn|javascript|typescript)\b',
        'browser': r'\b(browser|navegador|playwright|selenium|cypress)\b',
        'docker': r'\b(docker|podman|kubernetes)\b', 'pytest': r'\bpytest\b'}
    for capability, pattern in unsupported.items():
        if any(re.search(pattern, t, re.I) for t in descriptions):
            errors.append(capability + '_unsupported')
    if any(Path(p).suffix in ('.js', '.jsx', '.ts', '.tsx', '.rs', '.go', '.java') for p in policy['write_paths']):
        errors.append('non_python_implementation_unsupported')
    return sorted(set(errors))


def check_coverage(plan, slice_, definition):
    applicable = gates_for(plan, slice_['id'], 'before_slice') + gates_for(plan, slice_['id'], 'after_slice')
    checks = definition['checks']
    errors = []
    criteria = set()
    for gate in applicable:
        selected = [c for c in checks if c['gate'] == gate['id']]
        covered = {i for c in selected for i in c['gate_checks']}
        if covered != set(range(len(gate['checks']))):
            errors.append('verification_definition_missing:' + gate['id'])
        if gate['trigger'] == 'after_slice':
            criteria.update(i for c in selected for i in c['criteria'])
    if criteria != set(range(len(slice_['acceptance_criteria']))):
        errors.append('acceptance_coverage_missing')
    constructed = {h['id'] for h in plan['harness'] if h['introduced_by'] == slice_['id'] and h['when'] == 'during_slice'}
    if any(constructed & set(g['harness']) for g in applicable):
        independent = {i for c in checks if c['kind'] == 'python_behavior' and
                       c['gate'] in {g['id'] for g in applicable if g['trigger'] == 'after_slice'} for i in c['criteria']}
        if independent != set(range(len(slice_['acceptance_criteria']))):
            errors.append('independent_acceptance_oracle_missing')
    if not applicable:
        errors.append('local_verification_missing')
    return errors


def harness_errors(plan, slice_, definition, worktree, *, after=False):
    inventory = files(worktree)
    locations = {h['id']: h['paths'] for h in definition['harness']}
    applicable = gates_for(plan, slice_['id'], 'before_slice') + gates_for(plan, slice_['id'], 'after_slice')
    required = {h for g in applicable for h in g['harness']}
    errors = []
    for h in plan['harness']:
        if h['id'] not in required:
            continue
        if not after and h['introduced_by'] == slice_['id'] and h['when'] == 'during_slice':
            continue  # This is a deliverable, not a precondition.
        if not locations.get(h['id']) or any(p not in inventory for p in locations[h['id']]):
            errors.append('harness_unavailable:' + h['id'])
    return errors


def protected_harness(definition, inventory):
    paths = {p for h in definition['harness'] for p in h['paths']}
    paths.update(c['target'] for c in definition['checks'] if c['kind'] == 'python_unittest')
    return {p: inventory[p] for p in paths if p in inventory}


class Verifier:
    def __init__(self, sandbox, *, clean=False):
        self.sandbox = sandbox
        self.clean = clean

    def run(self, plan, slice_, definition, worktree, *, trigger, should_stop, on_process, remaining):
        identity = code_identity(worktree)
        gates = {g['id'] for g in gates_for(plan, slice_['id'], trigger)}
        evidence = []
        for c in definition['checks']:
            if c['gate'] not in gates:
                continue
            base = {'check_id': c['id'], 'gate': c['gate'], 'criteria': c['criteria'],
                    'gate_checks': c['gate_checks'], 'code_id': identity, 'trigger': trigger,
                    'integration_mode': c.get('integration_mode', 'local'),
                    'started_at': time.time(),
                    'runner_sha256': hashlib.sha256(Path(__file__).with_name('verification_runner.py').read_bytes()).hexdigest(),
                    'python_sha256': hashlib.sha256(Path('/usr/bin/python3').read_bytes()).hexdigest()}
            if c['kind'] in ('specialist', 'human_review'):
                evidence.append({**base, 'status': 'NOT_RUN', 'reason': c['kind'] + '_pending'})
                continue
            with tempfile.TemporaryDirectory(prefix='check-', dir=self.sandbox.directory) as tmp:
                spec = Path(tmp) / 'check.json'
                spec.write_text(json.dumps(c))
                result = self.sandbox.run(['/usr/bin/python3', '-I', *(['-S'] if self.clean else []), '/verification_runner.py'],
                    [(str(worktree), '/workspace', False), (str(spec), '/check.json', False),
                     (str(Path(__file__).with_name('verification_runner.py')), '/verification_runner.py', False)],
                    timeout=max(.01, min(c['timeout_seconds'], remaining())),
                    should_stop=should_stop, on_process=on_process, clean=self.clean)
            if result['exit_code'] == 124 and not result.get('reason'):
                result.update(status='NOT_RUN', reason='missing_dependency_or_tests')
            if result['status'] == 'PASS' and 'FACTORY_CHECK_COMPLETED_V1' not in result['log'].splitlines():
                result.update(status='NOT_RUN', reason='runner_did_not_complete')
            evidence.append({**base, **result})
            if code_identity(worktree) != identity:
                raise FactoryError('stale_evidence', 'Code changed during verification; old PASS cannot accept new code')
            if should_stop():
                break
        return evidence
