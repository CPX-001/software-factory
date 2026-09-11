"""Compile reviewed planning references into the existing execution contract.

No inference, runner, closure or budget engine lives here. Templates and resources
are authorized before planning; only their references can be proposed by the planner.
"""
from copy import deepcopy
import hashlib
from datetime import datetime, timezone

from .architecture import fingerprint
from .execution_contract import (allowed, check_schema, POLICY_SCHEMA, relative_path,
                                 validate_verification, VERIFICATION_SCHEMA)
from .execution_store import ExecutionStore
from .execution_workspace import files, git_bytes
from .registry import FactoryError


def prepare_templates(store, policy, definition):
    value = deepcopy(definition)
    check_schema(value, VERIFICATION_SCHEMA)
    if not value.get('project_acceptance'):
        raise FactoryError('acceptance_contract_missing', 'Automatic binding needs the authorized delivery and product-entry contract')
    ids = [c['id'] for c in value['checks']]
    authorizations = [a['id'] for a in value.get('scope_authorizations', [])]
    if not ids or len(ids) != len(set(ids)) or len(authorizations) != len(set(authorizations)):
        raise FactoryError('invalid_verification', 'Templates and scope authorizations need unique stable IDs')
    expected = {r['path']: r['sha256'] for r in value.get('resources', [])}
    if len(expected) != len(value.get('resources', [])):
        raise FactoryError('invalid_verification', 'Duplicate verification resource')
    paths = set(expected) | {c['target'] for c in value['checks'] if c['kind'] == 'python_unittest'}
    paths.update(p for h in value['harness'] for p in h['paths'])
    inventory = files(store.project)
    for path in sorted(paths):
        relative_path(path)
        if path not in inventory or allowed(path, policy['write_paths']):
            raise FactoryError('verification_resource_unavailable', 'Automatic binding requires existing resources outside worker write permissions: ' + path)
        digest = inventory[path]['sha256']
        # Worktree-only files cannot become independent authorized evidence accidentally.
        if hashlib.sha256(git_bytes(store.project, 'show', 'HEAD:' + path)).hexdigest() != digest:
            raise FactoryError('verification_weakened', 'Commit the reviewed verification resource before authorization: ' + path)
        if path in expected and expected[path] != digest:
            raise FactoryError('verification_weakened', 'Authorized verification resource changed: ' + path)
        expected[path] = digest
    for check in value['checks']:
        if check.get('integration_mode') == 'external_service' or check['kind'] == 'specialist':
            raise FactoryError('capability_unavailable', 'Unsupported mandatory verification cannot be bound automatically')
        if check['kind'] == 'python_unittest':
            digest = expected[check['target']]
            if check.get('source_sha256', digest) != digest:
                raise FactoryError('verification_weakened', 'Independent unittest hash differs from its authorized source')
            check['source_sha256'] = digest
    value['resources'] = [{'path': p, 'sha256': h} for p, h in sorted(expected.items())]
    return value


def context(store):
    policy = ExecutionStore(store).policy()
    if not policy.get('automatic_plan_binding') or not policy.get('analysis_authorized'):
        return None
    definition = ExecutionStore(store).definition(policy['definition_id'])['verification']
    from .verification import verify_resources
    verify_resources(definition, store.project)
    try:
        resources = {r['path']: (store.project / r['path']).read_text(encoding='utf-8') for r in definition.get('resources', [])}
    except UnicodeError as exc:
        raise FactoryError('verification_resource_unavailable', 'Planning review requires declared UTF-8 oracle resources') from exc
    if sum(len(text.encode()) for text in resources.values()) > 60000:
        raise FactoryError('verification_context_too_large', 'Consolidate the declared oracle resources; never truncate their review')
    return {'definition_id': policy['definition_id'], 'templates': definition,
            'resources': resources, 'permissions': {'write_paths': policy['write_paths']}}


def compile_binding(plan, source, templates, definition_id, policy, decisions=()):
    from .planning_contract import PLAN_SCHEMA
    binding = plan.get('execution_binding')
    if not binding:
        raise FactoryError('verification_binding_missing', 'Planning must bind its conditions to the preauthorized checks')
    check_schema(binding, PLAN_SCHEMA['properties']['execution_binding'])
    if binding['definition_id'] != definition_id:
        raise FactoryError('stale_verification_binding', 'Planning references a different verification authorization')
    definitions = {c['id']: c for c in templates['checks']}
    value = deepcopy(templates)
    value['checks'] = []
    for item in binding['checks']:
        if item['template'] not in definitions:
            raise FactoryError('verification_binding_invalid', 'Unknown authorized template: ' + item['template'])
        if item['id'] in definitions and item['id'] != item['template']:
            raise FactoryError('verification_weakened', 'An original check ID cannot be rebound to another oracle')
        value['checks'].append({**deepcopy(definitions[item['template']]),
            **{k: item[k] for k in ('id', 'gate', 'criteria', 'gate_checks')}})
    missing = set(definitions) - {c['id'] for c in value['checks']}
    if missing:
        raise FactoryError('verification_weakened', 'Every original check must retain its ID and procedure: ' + ', '.join(sorted(missing)),
                           details={'pending': ['original_check_id_missing:' + key for key in sorted(missing)]})
    available = {r['path'] for r in templates.get('resources', [])}
    value['harness'] = deepcopy(binding['harness'])
    if any(not set(h['paths']) <= available for h in value['harness']):
        raise FactoryError('verification_binding_invalid', 'Harness references resources not authorized before planning')
    requirements = {r['key']: r['text'] for r in source['requirements']}
    value['requirement_acceptance'] = []
    for item in binding['requirements']:
        if item['requirement'] not in requirements:
            raise FactoryError('verification_binding_invalid', 'Unknown requirement acceptance reference')
        value['requirement_acceptance'].append({**deepcopy(item), 'condition': requirements[item['requirement']]})
    authorized = {a['id']: a for a in templates.get('scope_authorizations', [])}
    coverage = {c['requirement']: c for c in plan['coverage']}
    exclusions = deepcopy(value['project_acceptance']['exclusions'])
    for item in binding['exclusions']:
        disposition = coverage.get(item['requirement'], {}).get('disposition')
        if 'decision_id' in item:
            from .project_validation import exclusion_decision_authorized
            decision = next((d for d in decisions if d['id'] == item['decision_id']), None)
            if disposition not in ('deferred', 'out_of_scope') or not exclusion_decision_authorized(decision, item['requirement'], disposition):
                raise FactoryError('prior_exclusion_authorization_missing', 'Exclusion needs the actual recorded human answer')
            exclusions.append(dict(item))
            continue
        approval = authorized.get(item['authorization'])
        if not approval or approval['disposition'] != disposition:
            raise FactoryError('prior_exclusion_authorization_missing', 'No preauthorized input for this exclusion and disposition')
        exclusions.append({'requirement': item['requirement'], 'prior_input': deepcopy(approval['prior_input'])})
    value['project_acceptance']['exclusions'] = exclusions
    validate_verification(value, plan)
    checks = {c['id']: c for c in value['checks']}
    gates = {g['id']: g for g in plan['gates']}
    errors = []
    for gate in plan['gates']:
        mapped = {i for c in value['checks'] if c['gate'] == gate['id'] for i in c['gate_checks']}
        if mapped != set(range(len(gate['checks']))):
            errors.append('verification_definition_missing:' + gate['id'])
    from .verification import check_coverage, capability_errors, outline_criteria
    for slice_ in plan['slices']:
        effective = {**slice_, 'acceptance_criteria': outline_criteria(plan, slice_)}
        errors.extend(check_coverage(plan, effective, value))
        errors.extend(capability_errors(plan, effective, value, policy, source['baseline']))
    from .milestone import closure_obligations
    for milestone in plan['milestones']:
        errors.extend(closure_obligations(plan, milestone, value)[2])
        strategic = [g for g in plan['gates'] if g['target'] == milestone['id']]
        projected = {**plan, 'gates': [{**g, 'trigger': 'after_slice'} for g in strategic]}
        target = {'id': milestone['id'], 'scope': [], 'verification_expectation': '',
                  'components': sorted({c for s in plan['slices'] if s['milestone'] == milestone['id'] for c in s['components']})}
        errors.extend(capability_errors(projected, target, value, policy, source['baseline']))
    bound_harness = {h['id'] for h in value['harness'] if h['paths']}
    for gate in plan['gates']:
        if not set(gate['harness']) <= bound_harness:
            errors.append('harness_unavailable:' + gate['id'])
    contracts = {c['requirement']: c for c in value['requirement_acceptance']}
    for key, item in coverage.items():
        if item['disposition'] == 'covered':
            contract = contracts.get(key)
            contributors = {m['id'] for m in plan['milestones'] if key in m['requirements']}
            if not contract or not contributors <= set(contract['milestones']):
                errors.append('full_requirement_acceptance_missing:' + key)
            elif len(contributors) > 1 and not any(gates[checks[c]['gate']]['trigger'] == 'project_close' for c in contract['checks']):
                errors.append('transversal_project_acceptance_missing:' + key)
        elif item['disposition'] in ('deferred', 'out_of_scope'):
            if not any(e['requirement'] == key for e in exclusions):
                errors.append('prior_exclusion_authorization_missing:' + key)
        else:
            errors.append('requirement_blocked:' + key)
    for key in value['project_acceptance']['entry_checks']:
        check = checks[key]
        if (gates[check['gate']]['trigger'] != 'project_close' or
                check['kind'] != 'python_behavior' and not check.get('entrypoint')):
            errors.append('real_product_entry_missing:' + key)
    if not value['project_acceptance']['entry_checks']:
        errors.append('product_entry_acceptance_missing')
    if errors:
        raise FactoryError('verification_binding_invalid', 'Approved conditions lack executable authorized coverage',
                           details={'pending': sorted(set(errors))})
    return value


def gate_errors(store, plan, source):
    try:
        bound = context(store)
        if not bound:
            return [], None
        value = compile_binding(plan, source, bound['templates'], bound['definition_id'], ExecutionStore(store).policy(), store.snapshot()['decisions'])
        from .project_validation import resolve_prior_inputs
        resolve_prior_inputs(store, plan, value, before=datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z'))
        return [], fingerprint(value)
    except FactoryError as exc:
        return [exc.code, *exc.details.get('pending', []), str(exc)], None


def adopt(store, run_id):
    """Called by the owning controller after normal planning acceptance, or recovery."""
    from .continuation_store import ContinuationStore
    from .execution import configure
    from .integrated_code import reconcile
    from .runtime import Runtime
    runtime = Runtime(store)
    group = ContinuationStore(store).latest()
    if not group or not group.get('analysis') or not group['policy'].get('automatic_plan_binding'):
        return False
    if runtime.state()['run_id'] != run_id or group['runtime_id'] != run_id:
        raise FactoryError('stale_run', 'Only the owning process may publish the reviewed binding')
    if runtime.paused():
        raise FactoryError('paused', 'Pause prevents automatic binding publication')
    snapshot = store.snapshot()
    roadmap = snapshot['planning']['roadmap']
    bound = context(store)
    value = compile_binding(roadmap['plan'], roadmap['source'], bound['templates'], bound['definition_id'], group['policy'], snapshot['decisions'])
    review = roadmap['review']
    if (not review['classification']['required'] or not review['passes'] or review['passes'][-1]['findings'] or
            roadmap['gate'].get('verification_binding') != fingerprint(value)):
        raise FactoryError('verification_binding_unreviewed', 'Automatic publication requires the accepted planning review of this exact binding')
    policy = {k: group['policy'][k] for k in POLICY_SCHEMA['properties'] if k in group['policy']}
    configure(store, policy, value, owner_run_id=run_id)
    journal = ContinuationStore(store)
    group = journal.latest()
    group.update(integrated=reconcile(store), state='queued')
    journal.save(group)
    with store._connection(write=True) as db:
        Runtime.event(db, 'automatic_verification_bound', {'run_id': run_id, 'continuation_id': group['id'],
            'definition_id': group['policy']['definition_id'], 'plan': group['sources']['plan']})
    return True
