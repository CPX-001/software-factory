"""Progressive planning: immutable roadmap revisions, bounded workers, deterministic gates."""
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import json
import time

from .architecture import canonical, fingerprint, classify as classify_architecture, SECTIONS
from .planning_contract import validate_plan, validate_review
from .workflow import WorkflowError

MAX_CALLS = 8
MAX_CONTEXT_BYTES = 180_000
MAX_OUTPUT_BYTES = 130_000


def call_limits(state):
    grant = state.get('authorized_recovery')
    if grant:
        return {'calls': grant['call_limit'], 'critic_passes': grant['critic_limit'],
                'reconciliations': grant['reconciliation_limit']}
    return {'calls': MAX_CALLS,
            'critic_passes': 2 + state.get('recovery_attempts', 0) + int(state.get('review_context_refreshed', False)),
            'reconciliations': 1 + state.get('recovery_attempts', 0)}


def recovery_replay(store, policy, verification, request):
    from .execution_contract import check_schema
    from .planning_contract import RECOVERY_SCHEMA
    from .continuation_store import ContinuationStore
    from .registry import FactoryError
    check_schema(request, RECOVERY_SCHEMA)
    group = ContinuationStore(store).latest()
    for grant in recovery_grants(group):
        if grant['request']['request_id'] == request['request_id']:
            if grant['request_fingerprint'] != fingerprint([policy, verification, request]):
                raise FactoryError('request_id_conflict', 'Recovery request ID already records different authorization')
            return grant['result']
    return None


def recovery_grants(group):
    group = group or {}
    return group.get('planning_recoveries', [group['planning_recovery']] if group.get('planning_recovery') else [])


def authorize_recovery(store, policy, verification, request):
    """One explicit operator correction/review grant, on the existing analysis ledger.

    Called under the service's launch/run locks. No plan edit, counter reset or
    implicit retry on ordinary resume; aggregate extensions are explicit and durable.
    """
    from .continuation_store import ContinuationStore, budget
    from .execution_contract import POLICY_SCHEMA
    from .execution_store import ExecutionStore
    from .registry import FactoryError
    from .runtime import Runtime
    from .verification import verify_resources
    replay = recovery_replay(store, policy, verification, request)
    if replay:
        return replay
    request_fingerprint = fingerprint([policy, verification, request])
    group = ContinuationStore(store).latest()
    snapshot = store.snapshot()
    state = {k: deepcopy(v) for k, v in snapshot['planning'].items() if k not in ('roadmap', 'decision_details')}
    grants = recovery_grants(group)
    extending = request.get('verification_extension', False)
    pending = any(d['answer'] is None for d in snapshot['decisions'])
    waiting_extension = extending and pending and state['stage'] in ('propose', 'reconcile')
    if (not group or not group.get('analysis') or any(
            g['request']['proposal_fingerprint'] == request['proposal_fingerprint'] for g in grants) or
            snapshot['phase'] != 'planning' or (state['stage'] != 'blocked' and not waiting_extension) or not state['proposal'] or
            snapshot['planning']['roadmap'] or group['accepted']):
        raise FactoryError('planning_recovery_unavailable', 'An operator grant needs a distinct unaccepted blocked proposal; replay or renaming cannot renew it')
    if request['run_id'] != group['runtime_id'] or request['proposal_fingerprint'] != fingerprint(state['proposal']):
        raise FactoryError('stale_sources', 'Recovery must name the current run and exact blocked proposal')
    if (pending and not waiting_extension) or source_snapshot(snapshot) != state['source']:
        raise FactoryError('stale_sources', 'Resolve human decisions or changed requirements/architecture before recovery')
    old_policy = {k: v for k, v in group['policy'].items() if k in POLICY_SCHEMA['properties']}
    mutable = {'continuation', 'context_paths'} if extending else {'continuation'}
    if ({k: v for k, v in policy.items() if k not in mutable} !=
            {k: v for k, v in old_policy.items() if k not in mutable}):
        raise FactoryError('policy_changed', 'Planning recovery preserves model, effort, permissions and individual limits')
    before, after = group['limits'], policy['continuation']
    extensible = {'max_seconds', 'max_calls', 'max_tokens'}
    if (any(after.get(k) != before.get(k) for k in set(before) | set(after) if k not in extensible) or
            any(after[k] < before[k] for k in extensible)):
        raise FactoryError('policy_changed', 'Recovery can explicitly extend aggregate calls, tokens and time; work limits and permissions remain fixed')
    journal = ExecutionStore(store)
    original = journal.definition(group['policy']['definition_id'])['verification']
    previous_definition = group['policy']['definition_id']
    identifier = previous_definition
    if extending:
        from .planning_binding import extend_templates
        verification = extend_templates(store, policy, original, verification)
        before_context, after_context = set(old_policy['context_paths']), set(policy['context_paths'])
        if not before_context <= after_context or not after_context - before_context <= {r['path'] for r in verification['resources']}:
            raise FactoryError('policy_changed', 'Additional context is limited to the newly authorized immutable evidence')
        identifier = fingerprint({'sources': None, 'verification': verification})
    elif verification != original:
        raise FactoryError('acceptance_contract_frozen', 'Recovery preserves every predeclared check, resource and scope authorization')
    verify_resources(verification, store.project)
    deadline = group['deadline'] + after['max_seconds'] - before['max_seconds']
    with store._connection() as db:
        remaining = budget(db, group['id'])
    if remaining['usage_unknown_calls']:
        raise FactoryError('usage_unknown', 'Unreported prior usage prevents another recovery call')
    if deadline <= time.time() or after['max_calls'] - remaining['calls'] < 2 or after['max_tokens'] <= remaining['tokens']:
        raise FactoryError('budget_exhausted', 'The reviewed recovery needs time, tokens and two calls within the same aggregate budget')
    now = time.time()
    limits = {'request_id': request['request_id'], 'call_limit': state['calls'] + 2,
              'critic_limit': state['critic_calls'] + 1, 'reconciliation_limit': state['reconciliations'] + 1}
    result = {'policy': {**group['policy'], 'continuation': after,
                         'context_paths': policy['context_paths'], 'definition_id': identifier},
              'definition_id': identifier, 'planning_recovery': limits,
              'continuation_id': group['id'], 'run_id': group['runtime_id'], 'deadline': deadline}
    grant = {'request': request, 'request_fingerprint': request_fingerprint,
             'authorized_at': now, 'result': result, 'previous_stage': state['stage'],
             'previous_gate': state.get('gate'), 'previous_blockers': state['blockers'],
             'calls_consumed': state['calls'], 'critic_calls_consumed': state['critic_calls']}
    amendment = {'continuation_id': group['id'], 'before': before, 'after': after,
                 'previous_deadline': group['deadline'], 'deadline': deadline, 'authorized_at': now,
                 'request_id': request['request_id']}
    state.update(stage='reconcile', authorized_recovery=limits, reconciliations=state['reconciliations'] + 1)
    state['classification']['required'] = True
    group.update(policy=result['policy'], limits=after, deadline=deadline, planning_recovery=grant,
                 planning_recoveries=[*grants, grant])
    if extending:
        extension = {'previous_definition': previous_definition, 'definition_id': identifier,
                     'request_id': request['request_id'], 'authorized_at': now,
                     'checks_added': sorted({c['id'] for c in verification['checks']} - {c['id'] for c in original['checks']})}
        group.setdefault('verification_extensions', []).append(extension)
    if before != after:
        group.setdefault('budget_amendments', []).append(amendment)
    with store._connection(write=True) as db:
        if store._check(db, snapshot['revision']) != 'planning':
            raise FactoryError('stale_sources', 'Planning changed before recovery authorization')
        if db.execute('SELECT run_id FROM factory_control WHERE id=1').fetchone()[0] != request['run_id']:
            raise FactoryError('stale_run', 'Recovery cannot replace a newer process owner')
        if extending:
            db.execute('INSERT OR IGNORE INTO execution_definitions VALUES (?,?,?)',
                       (identifier, canonical({'sources': None, 'verification': verification}), now))
            Runtime.event(db, 'verification_evidence_extended', extension)
        db.execute('UPDATE continuations SET data=? WHERE id=?', (canonical(group), group['id']))
        db.execute('UPDATE execution_policy SET data=? WHERE id=1', (canonical(result['policy']),))
        Planning(store)._save(db, state, snapshot['revision'], 'planning_recovery_authorized')
        Runtime.event(db, 'planning_recovery_authorized', grant)
        if before != after:
            Runtime.event(db, 'workflow_budget_extended', amendment)
    return result


def migrate(db):
    for statement in (
        '''CREATE TABLE IF NOT EXISTS planning_run (
            id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS planning_calls (
            id INTEGER PRIMARY KEY, role TEXT NOT NULL, context TEXT NOT NULL,
            response TEXT, error TEXT, status TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))''',
        '''CREATE TABLE IF NOT EXISTS planning_decisions (
            key TEXT PRIMARY KEY, decision_id INTEGER UNIQUE NOT NULL REFERENCES decisions(id), data TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS planning_revisions (
            revision INTEGER PRIMARY KEY, parent_revision INTEGER REFERENCES planning_revisions(revision),
            architecture_revision INTEGER NOT NULL REFERENCES architecture_baselines(revision),
            architecture_fingerprint TEXT NOT NULL, fingerprint TEXT NOT NULL, plan TEXT NOT NULL,
            source TEXT NOT NULL, review TEXT NOT NULL, gate TEXT NOT NULL, reason TEXT NOT NULL,
            projection TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))''',
        '''CREATE TABLE IF NOT EXISTS planning_current (
            id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL REFERENCES planning_revisions(revision))''',
        # Append-only even if a future caller accidentally uses UPDATE/DELETE.
        '''CREATE TRIGGER IF NOT EXISTS planning_no_update BEFORE UPDATE ON planning_revisions
            BEGIN SELECT RAISE(ABORT, 'Planning revisions are immutable'); END''',
        '''CREATE TRIGGER IF NOT EXISTS planning_no_delete BEFORE DELETE ON planning_revisions
            BEGIN SELECT RAISE(ABORT, 'Planning revisions are immutable'); END''',
    ):
        db.execute(statement)
    db.execute('PRAGMA user_version=5')


def read_state(db):
    result = {'stage': 'not_started', 'calls': 0, 'blockers': [], 'roadmap': None,
              'decision_details': [], 'proposal': None, 'review': None}
    if db.execute('PRAGMA user_version').fetchone()[0] < 5:
        return result
    row = db.execute('SELECT data FROM planning_run WHERE id=1').fetchone()
    if row:
        result.update(json.loads(row[0]))
    row = db.execute('''SELECT r.* FROM planning_revisions r JOIN planning_current c
                        ON r.revision=c.revision WHERE c.id=1''').fetchone()
    if row:
        result['roadmap'] = dict(row)
        for key in ('plan', 'source', 'review', 'gate'):
            result['roadmap'][key] = json.loads(row[key])
    result['decision_details'] = [dict(json.loads(r['data']), id=r['decision_id'], answer=r['answer'])
        for r in db.execute('''SELECT p.*, d.answer FROM planning_decisions p JOIN decisions d
                              ON d.id=p.decision_id ORDER BY d.id''')]
    return result


def bounded(value, limit=MAX_CONTEXT_BYTES):
    if len(canonical(value).encode()) > limit:
        raise WorkflowError(f'Planning context exceeds {limit} bytes; consolidate explicitly, never truncate')
    return value


def source_snapshot(snapshot):
    baseline = snapshot['architecture']['baseline']
    if not baseline or not baseline['gate']['passed'] or fingerprint(baseline['architecture']) != baseline['fingerprint']:
        raise WorkflowError('Planning requires a valid accepted architecture baseline')
    facts = [{k: i[k] for k in ('key', 'category', 'text', 'status', 'basis')}
             for i in snapshot['discovery']['knowledge'] if i['status'] != 'superseded']
    if any(i['blocking'] for i in snapshot['discovery']['knowledge'] if i['status'] != 'superseded'):
        raise WorkflowError('Planning requirements contain blockers')
    if not facts or any(i['status'] not in ('known', 'assumption') for i in facts):
        raise WorkflowError('Planning requires active authoritative requirements')
    # Human answers incorporated into current facts/ADRs are relevant; old decision history is not.
    current_adrs = [a for a in snapshot['architecture']['adrs'] if a['baseline_revision'] == baseline['revision']]
    relevant_decisions = {a['human_decision_id'] for a in current_adrs if a['human_decision_id'] is not None}
    relevant_keys = {f['key'] for f in facts} | {f['key'] for f in baseline['source_snapshot']['knowledge']}
    relevant_decisions.update(d['id'] for d in snapshot['decisions'] if f"human_decision_{d['id']}" in relevant_keys)
    source = {'requirements': facts, 'architecture': {k: baseline[k] for k in ('revision', 'fingerprint')},
              'baseline': baseline['architecture'],
              'adrs': [a['decision'] for a in current_adrs],
              'decisions': [{'id': d['id'], 'question': d['question'], 'answer': d['answer']}
                            for d in snapshot['decisions'] if d['answer'] is not None and d['id'] in relevant_decisions]}
    return bounded(source, 100_000)


def classify(source, plan=None):
    result = classify_architecture({'knowledge': source['requirements']}, source['baseline'])
    if plan and (len(plan['milestones']) > 2 or len(plan['slices']) > 5 or
                 any(r['severity'] in ('high', 'critical') for r in plan['risks']) or
                 any(s['architecture_impact'] == 'potential_change' for s in plan['slices'])):
        result.update(required=True, risk='high' if result['risk'] != 'critical' else 'critical')
        result['reasons'].append('planning_complexity_or_risk')
    return result


def executable_slices(plan):
    milestones = {m['id']: m for m in plan['milestones']}
    completed = {s['id'] for s in plan['slices'] if s['maturity'] == 'completed'}
    closed = {m['id'] for m in plan['milestones'] if m['status'] == 'completed'}
    near = set(plan['near_term'])
    return [s for s in plan['slices'] if s['id'] in near and s['maturity'] == 'execution_ready'
            and set(s['dependencies']) <= completed and s['milestone'] in milestones
            and milestones[s['milestone']]['status'] == 'open'
            and set(milestones[s['milestone']]['dependencies']) <= closed]


def quality_gate(plan, source, decisions=(), review_passed=True, *, initial=True):
    validate_plan(plan)
    errors = []
    def require(ok, error):
        if not ok:
            errors.append(error)
    def refs(values, allowed, error, nonempty=False):
        require((bool(values) or not nonempty) and len(values) == len(set(values)) and set(values) <= set(allowed), error)
    groups = {k: {e['id']: e for e in plan[k]} for k in ('milestones', 'slices', 'gates', 'harness', 'risks')}
    milestones, slices, gates, harness, risks = (groups[k] for k in groups)
    ids = [e['id'] for k in groups for e in plan[k]]
    require(len(ids) == len(set(ids)), 'duplicate_ids')
    requirements = {r['key'] for r in source['requirements']}
    components = {c['id'] for c in source['baseline']['components']}
    boundaries = {e['id'] for name in SECTIONS for e in source['baseline'][name]}
    require(plan['architecture'] == source['architecture'], 'architecture_binding')
    require(bool(milestones), 'milestones_missing')
    require(bool(executable_slices(plan)), 'no_initial_executable_slice')
    refs(plan['near_term'], slices, 'near_term_references', True)
    require(len(plan['near_term']) <= 3, 'detail_horizon')
    # Expand milestone prerequisites to slices: catches cross-level cycles and invalid paths.
    graph = {k: set(s['dependencies']) for k, s in slices.items()}
    mgraph = {k: set(m['dependencies']) for k, m in milestones.items()}
    for sid, s in slices.items():
        m = milestones.get(s['milestone'])
        if m:
            graph[sid].update(t['id'] for t in slices.values() if t['milestone'] in m['dependencies'])
    def ancestors(graph):
        result, active = {}, set()
        def visit(node):
            if node in active:
                require(False, 'dependency_cycle')
                return set()
            if node in result:
                return result[node]
            active.add(node)
            found = set(graph.get(node, ()))
            for parent in graph.get(node, ()):
                found.update(visit(parent))
            active.remove(node)
            result[node] = found
            return found
        for node in graph:
            visit(node)
        return result
    ancestors(mgraph)
    prior = ancestors(graph)
    for mid, m in milestones.items():
        require(m['architecture'] == source['architecture'], 'architecture_binding:' + mid)
        refs(m['requirements'], requirements, 'milestone_requirements:' + mid, True)
        refs(m['dependencies'], milestones, 'milestone_dependencies:' + mid)
        refs(m['risks'], risks, 'milestone_risks:' + mid)
        require(bool(m['success_criteria']) and bool(m['closure_conditions']), 'milestone_criteria:' + mid)
        require(all(i < len(m['success_criteria'] + m['closure_conditions']) for i in m.get('subjective_criteria', [])), 'subjective_criteria:' + mid)
        refs(m['verification_gates'], gates, 'milestone_gates:' + mid, True)
        require(not any(gates[gid]['trigger'] == 'project_close' for gid in m['verification_gates'] if gid in gates),
                'project_close_before_milestone:' + mid)
        require(any(g['kind'] == 'milestone' and g['target'] == mid and g['id'] in m['verification_gates']
                    for g in gates.values()), 'milestone_gate_missing:' + mid)
        require(any(s['milestone'] == mid for s in slices.values()), 'milestone_slices:' + mid)
        if initial:
            require(m['status'] != 'completed', 'premature_completion:' + mid)
    for sid, s in slices.items():
        require(s['milestone'] in milestones, 'slice_milestone:' + sid)
        require(s['architecture'] == source['architecture'], 'architecture_binding:' + sid)
        refs(s['requirements'], requirements, 'slice_requirements:' + sid, s['kind'] == 'vertical')
        refs(s['dependencies'], slices, 'slice_dependencies:' + sid)
        refs(s['components'], components, 'slice_components:' + sid, s['maturity'] == 'execution_ready')
        refs(s['boundaries'], boundaries, 'slice_boundaries:' + sid)
        refs(s['risks'], risks, 'slice_risks:' + sid)
        require(s['architecture_impact'] != 'potential_change' or bool(s['impact_reason'].strip()), 'impact_reason:' + sid)
        if initial:
            require(s['maturity'] != 'completed', 'premature_completion:' + sid)
        if s['maturity'] == 'execution_ready':
            require(sid in plan['near_term'], 'detail_outside_horizon:' + sid)
            require(bool(s['scope']) and bool(s['acceptance_criteria']) and bool(s['verification_expectation'].strip()), 'slice_not_ready:' + sid)
            require(any(g['kind'] == 'local' and g['target'] == sid for g in gates.values()), 'local_gate_missing:' + sid)
            require(all(slices[d]['maturity'] == 'completed' or
                        slices[d]['maturity'] == 'execution_ready' and d in plan['near_term']
                        for d in prior.get(sid, ()) if d in slices),
                    'unrefined_dependency:' + sid)
        if s['kind'] == 'risk_probe':
            require(bool(s['risks']) and any(r['validation_slice'] == sid for r in risks.values()), 'unowned_probe:' + sid)
        for signal in s['verification_triggers']:
            require(any(g['kind'] == 'integration' and g['target'] == sid and signal in g['signals'] for g in gates.values()),
                    'integration_gate_missing:' + sid + ':' + signal)
    coverage = {}
    for c in plan['coverage']:
        key = c['requirement']
        require(key not in coverage and key in requirements, 'coverage_identity:' + key)
        coverage[key] = c
        require(c['milestone'] in milestones, 'coverage_owner:' + key)
        refs(c['slices'], slices, 'coverage_slices:' + key, c['disposition'] == 'covered')
        require(c['disposition'] != 'blocked', 'blocked_requirement:' + key)
        if c['milestone'] in milestones:
            require(key in milestones[c['milestone']]['requirements'], 'coverage_milestone_link:' + key)
        for sid in c['slices']:
            if sid in slices:
                require(key in slices[sid]['requirements'] and
                        key in milestones.get(slices[sid]['milestone'], {}).get('requirements', []), 'coverage_slice_link:' + key)
    for key in requirements:
        require(key in coverage, 'unowned_requirement:' + key)
    for mid, m in milestones.items():
        for key in m['requirements']:
            require(key in coverage and (coverage[key]['milestone'] == mid or
                    any(slices[s]['milestone'] == mid for s in coverage[key]['slices'] if s in slices)), 'reverse_milestone_coverage:' + key)
    for sid, s in slices.items():
        for key in s['requirements']:
            require(key in coverage and sid in coverage[key]['slices'], 'reverse_slice_coverage:' + key)
    accepted = {d.get('key') for d in decisions if (d.get('answer') or '').strip().lower() == 'accept'}
    for r in source['baseline']['risks']:
        require(r['id'] in risks, 'baseline_risk_missing:' + r['id'])
        if r['id'] in risks:
            order = ('low', 'medium', 'high', 'critical')
            require(order.index(risks[r['id']]['severity']) >= order.index(r['severity']), 'risk_downgraded:' + r['id'])
    for rid, r in risks.items():
        require(r['owner'] in milestones or r['owner'] in slices, 'risk_owner:' + rid)
        owner = milestones.get(r['owner'], slices.get(r['owner'], {}))
        require(rid in owner.get('risks', []), 'risk_owner_link:' + rid)
        refs(r['blocks'], slices, 'risk_blocks:' + rid)
        accepted_risk = r['acceptance_key'] in accepted
        if r['severity'] in ('high', 'critical'):
            require(bool(r['mitigation'].strip()) or accepted_risk, 'unmitigated_risk:' + rid)
            if not accepted_risk:
                require(r['validation_slice'] in slices, 'risk_validation:' + rid)
                if r['severity'] == 'critical':
                    require((r['validation_slice'] in plan['near_term'] and
                             slices.get(r['validation_slice'], {}).get('maturity') == 'execution_ready') or
                            slices.get(r['validation_slice'], {}).get('maturity') == 'completed', 'critical_risk_not_early:' + rid)
        if r['validation_slice']:
            require(r['validation_slice'] in slices and rid in slices[r['validation_slice']]['risks'], 'risk_validation_link:' + rid)
            for sid in r['blocks']:
                require(r['validation_slice'] in prior.get(sid, set()), 'risk_order:' + rid + ':' + sid)
    for gid, g in gates.items():
        require(bool(g['checks']) and bool(g['signals']), 'gate_strategy:' + gid)
        refs(g['harness'], harness, 'gate_harness:' + gid)
        if g['kind'] == 'local':
            require(g['cost'] == 'cheap' and g['trigger'] == 'after_slice' and g['target'] in slices, 'local_gate_cost_or_trigger:' + gid)
        elif g['kind'] == 'integration':
            require(g['trigger'] in ('before_slice', 'after_slice') and g['target'] in slices and
                    bool(set(g['signals']) & {'cross_component', 'boundary', 'persistence', 'security', 'public_api'}), 'integration_strategy:' + gid)
            if g['target'] in slices:
                require(set(g['signals']) <= set(slices[g['target']]['verification_triggers']), 'unjustified_integration:' + gid)
        else:
            require(g['target'] in milestones and g['trigger'] in (('milestone_close',) if g['kind'] == 'milestone' else ('project_checkpoint', 'project_close')), 'strategic_trigger:' + gid)
    for hid, h in harness.items():
        intro = slices.get(h['introduced_by'])
        require(bool(intro) and intro['milestone'] == h['milestone'], 'harness_introduction:' + hid)
        refs(h['needed_by_gates'], gates, 'harness_consumers:' + hid, True)
        actual = {g['id'] for g in gates.values() if hid in g['harness']}
        require(actual == set(h['needed_by_gates']), 'harness_gate_links:' + hid)
        for gid in h['needed_by_gates']:
            if gid not in gates or not intro:
                continue
            g = gates[gid]
            if g['target'] in slices:
                same = g['target'] == h['introduced_by']
                require((same and (g['trigger'] == 'after_slice' or h['when'] == 'before_slice')) or
                        h['introduced_by'] in prior.get(g['target'], set()), 'harness_too_late:' + hid + ':' + gid)
            elif g['target'] in milestones:
                targets = [s['id'] for s in slices.values() if s['milestone'] == g['target']]
                require(intro['milestone'] == g['target'] or any(h['introduced_by'] in prior.get(s, set()) for s in targets), 'harness_too_late:' + hid + ':' + gid)
    require(not plan['blockers'], 'unresolved_blockers')
    require(not plan['unresolved_questions'], 'unresolved_questions')
    require(not any(d.get('answer') is None for d in decisions), 'pending_human_decisions')
    answers = {d['key'] for d in decisions if d.get('key') and d.get('answer')}
    refs(plan['decision_keys'], answers, 'unknown_decision_answer')
    for key in answers:
        if any(d.get('key') == key and d.get('origin') == 'planning' for d in decisions):
            require(key in plan['decision_keys'], 'human_answer_not_incorporated:' + key)
    require(review_passed, 'review_not_passed')
    return {'passed': not errors, 'errors': sorted(set(errors))}


def projection(plan, revision, digest):
    lines = [f'# Roadmap · revision {revision}', '', plan['objective'], '',
             f"Architecture {plan['architecture']['revision']} · {plan['architecture']['fingerprint']}",
             '', f'Plan fingerprint: {digest}', '', '## Milestones', '']
    for m in plan['milestones']:
        lines += [f"### {m['id']} · {m['title']} ({m['status']})", '', m['objective'], '',
                  'Resultado: ' + m['observable_outcome'], '', 'Success criteria:']
        lines += ['- ' + c for c in m['success_criteria']]
        lines += ['', 'Dependencies: ' + ', '.join(m['dependencies']), '']
        for s in plan['slices']:
            if s['milestone'] == m['id']:
                lines += [f"- **{s['id']} · {s['title']}** — {s['maturity']} / {s['kind']}: {s['objective']}"]
        lines += ['']
    lines += ['## Next executable slices', ''] + ['- ' + s['id'] for s in executable_slices(plan)]
    # Lossless details remain inspectable while the opening roadmap is quick to scan.
    for section in ('slices', 'gates', 'harness', 'coverage', 'risks', 'milestones'):
        lines += ['', '## ' + section.title(), '', '```json', json.dumps(plan[section], ensure_ascii=False, indent=2), '```']
    return '\n'.join(lines) + '\n'


def append_revision(db, plan, source, review, gate, reason, *, expected_parent=None):
    """Persistence primitive for a future replan controller; no autonomous replan policy."""
    validate_plan(plan)
    if not reason.strip():
        raise WorkflowError('A revision requires an explicit reason')
    current = db.execute('SELECT revision FROM planning_current WHERE id=1').fetchone()
    parent = current[0] if current else None
    if parent != expected_parent:
        raise WorkflowError('Stale planning revision')
    baseline = db.execute('''SELECT b.* FROM architecture_baselines b JOIN architecture_current c
                             ON c.revision=b.revision WHERE c.id=1''').fetchone()
    if not baseline or source['architecture'] != {'revision': baseline['revision'], 'fingerprint': baseline['fingerprint']}:
        raise WorkflowError('Architecture changed since planning snapshot')
    if canonical(source['baseline']) != canonical(json.loads(baseline['architecture'])):
        raise WorkflowError('Planning source is not the accepted baseline')
    if plan['architecture'] != source['architecture']:
        raise WorkflowError('Roadmap architecture binding does not match its source')
    for section, status_key in (('milestones', 'status'), ('slices', 'maturity')):
        for element in plan[section]:
            binding = element['architecture']
            known = db.execute('SELECT fingerprint FROM architecture_baselines WHERE revision=?', (binding['revision'],)).fetchone()
            if not known or known[0] != binding['fingerprint'] or (element[status_key] != 'completed' and binding != source['architecture']):
                raise WorkflowError('Invalid entity architecture binding: ' + element['id'])
    if parent:
        old = json.loads(db.execute('SELECT plan FROM planning_revisions WHERE revision=?', (parent,)).fetchone()[0])
        for section, status in (('milestones', 'status'), ('slices', 'maturity')):
            new = {e['id']: e for e in plan[section]}
            for e in old[section]:
                if e[status] == 'completed' and new.get(e['id']) != e:
                    raise WorkflowError('Completed planning work is immutable: ' + e['id'])
                if section == 'milestones' and e[status] == 'completed':
                    if [s for s in old['slices'] if s['milestone'] == e['id']] != [s for s in plan['slices'] if s['milestone'] == e['id']]:
                        raise WorkflowError('Completed milestone membership is immutable')
    revision = (parent or 0) + 1
    digest = fingerprint(plan)
    db.execute('''INSERT INTO planning_revisions(revision,parent_revision,architecture_revision,
        architecture_fingerprint,fingerprint,plan,source,review,gate,reason,projection)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)''', (revision, parent, source['architecture']['revision'],
        source['architecture']['fingerprint'], digest, canonical(plan), canonical(source), canonical(review),
        canonical(gate), reason, projection(plan, revision, digest)))
    db.execute('INSERT OR REPLACE INTO planning_current VALUES (1,?)', (revision,))
    return revision


def export_roadmap(store):
    roadmap = store.snapshot()['planning']['roadmap']
    if roadmap:
        path = store.path.parent / 'planning'
        path.mkdir(exist_ok=True)
        temporary = path / 'ROADMAP.md.tmp'
        temporary.write_text(roadmap['projection'], encoding='utf-8')
        temporary.replace(path / 'ROADMAP.md')


def refinement_snapshot(snapshot, slice_id, evidence=()):
    """Compact input for future refinement; no history, transcript, SQL or unrelated slices."""
    roadmap = snapshot['planning']['roadmap']
    if not roadmap:
        raise WorkflowError('No roadmap to refine')
    plan = roadmap['plan']
    target = next((s for s in plan['slices'] if s['id'] == slice_id), None)
    if target is None or target['maturity'] == 'completed':
        raise WorkflowError('Refinement requires a non-completed slice')
    source = source_snapshot(snapshot)
    related = set(target['requirements'])
    mapped = {t for c in source['baseline']['coverage'] if c['source_key'] in related for t in c['targets']}
    component_ids = set(target['components']) | {c['id'] for c in source['baseline']['components'] if c['id'] in mapped}
    elements = {e['id'] for name in SECTIONS for e in source['baseline'][name]
                if e.get('id') in component_ids or component_ids & set(e.get('components', e.get('participants', [])))}
    elements.update(target['boundaries'])
    elements.update(mapped)
    elements.update(e['id'] for name in ('constraints', 'security', 'risks', 'assumptions') for e in source['baseline'][name])
    # Include dependency/contract/flow records touching affected components.
    for name in ('dependencies', 'contracts', 'data', 'integrations', 'information_flows'):
        for e in source['baseline'][name]:
            touched = {e.get(k) for k in ('source', 'target', 'owner', 'component')} | set(e.get('participants', [])) | set(e.get('steps', []))
            if component_ids & touched:
                elements.add(e['id'])
                if e.get('contract'):
                    elements.add(e['contract'])
    relevant_adrs = [a for a in source['adrs'] if set(a['evidence']) & related]
    return bounded({'architecture': source['architecture'], 'planned_architecture': target['architecture'],
        'roadmap_revision': roadmap['revision'], 'slice': target,
        'milestone': next(m for m in plan['milestones'] if m['id'] == target['milestone']),
        'requirements': [r for r in source['requirements'] if r['key'] in related or
                         r['category'] in ('constraints', 'security', 'success', 'nonfunctional')],
        'baseline': {'style': source['baseline']['style'], **{name: [e for e in source['baseline'][name] if e['id'] in elements] for name in SECTIONS}},
        'adrs': relevant_adrs, 'decisions': source['decisions'],
        'dependencies': [s for s in plan['slices'] if s['id'] in target['dependencies']],
        'pending_requirements': [c for c in plan['coverage'] if c['disposition'] != 'out_of_scope' and
                                (not c['slices'] or any(s['id'] in c['slices'] and s['maturity'] != 'completed' for s in plan['slices']))],
        'risks': [r for r in plan['risks'] if r['id'] in target['risks']],
        'verification': [g for g in plan['gates'] if g['target'] in (slice_id, target['milestone'])],
        'harness': [h for h in plan['harness'] if h['introduced_by'] == slice_id or any(
            g['target'] == slice_id and h['id'] in g['harness'] for g in plan['gates'])],
        'evidence': list(evidence)}, 100_000)


class Planning:
    def __init__(self, store, model=None, router=None, should_stop=None, allow_recovery=False):
        self.store, self.model, self.router = store, model, router
        self.should_stop = should_stop or (lambda: False)
        self.allow_recovery = allow_recovery

    def _gate(self, plan, source, decisions=(), review_passed=True):
        gate = quality_gate(plan, source, decisions, review_passed)
        if gate['passed']:
            from .planning_binding import gate_errors
            errors, binding = gate_errors(self.store, plan, source)
            gate['errors'].extend(errors)
            gate['passed'] = not gate['errors']
            if binding:
                gate['verification_binding'] = binding
        return gate

    def queue_recovery(self):
        """One additional plan correction, charged to an explicitly authorized workflow.

        Keep proposal/reviews/call history and the accepted architecture untouched.
        A fresh resume cannot open another recovery allowance.
        """
        snapshot = self.store.snapshot()
        planning = snapshot['planning']
        if (not self.allow_recovery or planning.get('authorized_recovery') or snapshot['phase'] != 'planning' or planning['stage'] != 'blocked'
                or any(d['answer'] is None for d in snapshot['decisions'])):
            return False
        with self.store._connection() as db:
            previous_review = db.execute("SELECT context FROM planning_calls WHERE role LIKE 'critic%' AND status='completed' ORDER BY id DESC LIMIT 1").fetchone()
        refresh_review = bool(previous_review and not planning.get('review_context_refreshed')
                              and 'runtime_semantics' not in json.loads(previous_review[0])['input'])
        if (planning.get('recovery_attempts') and not refresh_review or
                planning.get('calls', 0) + (1 if refresh_review else 2) > MAX_CALLS):
            return False
        review_failure = any(b.startswith('Unresolved review ') for b in planning['blockers'])
        gate_failure = bool(planning.get('gate', {}).get('errors'))
        current = source_snapshot(snapshot)
        if (not (review_failure or gate_failure) or any(current[k] != planning['source'][k]
                                                     for k in ('architecture', 'requirements'))):
            return False
        from .continuation_store import ContinuationStore, check_budget
        group = ContinuationStore(self.store).latest()
        if not group or not group.get('analysis'):
            return False
        with self.store._connection() as db:
            remaining = check_budget(db, group['id'], dispatch=True)
        if remaining['calls_remaining'] < (1 if refresh_review else 2) or remaining['usage_unknown_calls']:
            return False
        state = {k: deepcopy(v) for k, v in planning.items() if k not in ('roadmap', 'decision_details')}
        if refresh_review:
            state.update(stage='critic_final', review_context_refreshed=True)
        else:
            state.update(stage='reconcile', recovery_attempts=1, reconciliations=state['reconciliations'] + 1)
        self._mutate(snapshot, state, 'planning_review_context_refreshed' if refresh_review else 'planning_bounded_recovery_queued')
        return True

    @contextmanager
    def _lock(self):
        with (self.store.path.parent / 'planning.lock').open('a') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise WorkflowError('Planning is already running in another process') from exc
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _save(self, db, state, revision, kind):
        db.execute('INSERT OR REPLACE INTO planning_run VALUES (1,?)', (canonical(state),))
        self.store._record(db, revision + 1, kind, {'stage': state['stage']})

    def _mutate(self, snapshot, state, kind, operation=None):
        with self.store._connection(write=True) as db:
            if self.store._check(db, snapshot['revision']) != 'planning':
                raise WorkflowError('Planning is no longer active')
            if operation:
                operation(db)
            self._save(db, state, snapshot['revision'], kind)

    @staticmethod
    def _request(db, question):
        old = db.execute('SELECT data FROM planning_decisions WHERE key=?', (question['key'],)).fetchone()
        if old:
            if json.loads(old[0]) != question:
                raise WorkflowError('Planning decision key cannot change meaning: ' + question['key'])
            return
        identifier = db.execute('INSERT INTO decisions(question) VALUES (?)', (question['question'],)).lastrowid
        db.execute('INSERT INTO planning_decisions VALUES (?,?,?)', (question['key'], identifier, canonical(question)))

    def run(self):
        self.store.initialize()
        with self._lock():
            result = self._run()
            export_roadmap(self.store)  # Rebuildable after a crash between SQLite commit and file rename.
            return result

    def _run(self):
        while True:
            snapshot = self.store.snapshot()
            planning = snapshot['planning']
            if self.should_stop() or planning['roadmap']:
                return snapshot
            if snapshot['phase'] != 'planning':
                raise WorkflowError('Planning can run only after architecture')
            if any(d['answer'] is None for d in snapshot['decisions']):
                return snapshot
            if planning['stage'] == 'not_started':
                source = source_snapshot(snapshot)
                state = {'stage': 'propose', 'calls': 0, 'critic_calls': 0, 'blockers': [],
                         'source': source, 'source_fingerprint': fingerprint(source),
                         'proposal': None, 'review': None, 'reviews': [], 'reconciliations': 0,
                         'classification': classify(source), 'routing': None}
                from .execution_store import ExecutionStore
                if ExecutionStore(self.store).policy().get('automatic_plan_binding'):
                    state['classification']['required'] = True
                self._mutate(snapshot, state, 'planning_started')
                continue
            state = {k: deepcopy(v) for k, v in planning.items() if k not in ('roadmap', 'decision_details')}
            if state['stage'] in ('blocked', 'completed'):
                if state['stage'] == 'blocked' and self.queue_recovery():
                    continue
                return snapshot
            current_source = source_snapshot(snapshot)
            if any(current_source[k] != state['source'][k] for k in ('architecture', 'requirements')):
                state.update(stage='blocked', blockers=['Architecture or requirements changed during planning; explicit replan required'])
                self._mutate(snapshot, state, 'planning_stale_architecture')
                return self.store.snapshot()
            with self.store._connection() as db:
                interrupted = db.execute("SELECT 1 FROM planning_calls WHERE status='pending'").fetchone()
            if interrupted:
                self._mutate(snapshot, state, 'planning_interrupted_calls', lambda db: db.execute(
                    "UPDATE planning_calls SET status='interrupted', error='Interrupted before checkpoint' WHERE status='pending'"))
                continue
            if state['stage'] == 'gate':
                decisions = self._decisions(snapshot)
                review_passed = not state['classification']['required'] or bool(state['reviews']) and not state['reviews'][-1]['findings']
                gate = self._gate(state['proposal'], state['source'], decisions, review_passed)
                state['gate'] = gate
                if not gate['passed']:
                    if not state['reconciliations']:
                        state.update(stage='reconcile', reconciliations=1, blockers=gate['errors'])
                    else:
                        state.update(stage='blocked', blockers=gate['errors'])
                    self._mutate(snapshot, state, 'planning_gate_failed')
                    continue
                self._complete(snapshot, state)
                return self.store.snapshot()
            if self._recover_completed_call(snapshot, state):
                continue
            limits = call_limits(state)
            if state['calls'] >= limits['calls'] or state['stage'].startswith('critic') and state['critic_calls'] >= limits['critic_passes']:
                state.update(stage='blocked', blockers=['Planning persistent call/review limit reached; explicit new effort required'])
                self._mutate(snapshot, state, 'planning_budget_exhausted')
                return self.store.snapshot()
            self._call(snapshot, state)

    @staticmethod
    def _decisions(snapshot):
        details = [dict(d, origin='planning') for d in snapshot['planning']['decision_details']]
        relevant = {d['id'] for d in source_snapshot(snapshot)['decisions']}
        ids = {d['id'] for d in details}
        details += [dict(d, origin='architecture') for d in snapshot['architecture']['decision_details'] if d['id'] not in ids and d['id'] in relevant]
        ids = {d['id'] for d in details}
        details += [d for d in snapshot['decisions'] if d['id'] not in ids and (d['id'] in relevant or d['answer'] is None)]
        return details

    def _call(self, snapshot, state):
        from .skill_catalog import discover_catalog
        from .skill_router import SkillRouter, Work, load_config
        role = state['stage']
        try:
            if self.router is None:
                roots, policy = load_config(self.store.project)
                catalog = discover_catalog(self.store.project, roots)
                if catalog.errors:
                    raise WorkflowError('; '.join(catalog.errors))
                self.router = SkillRouter(catalog, policy)
            concerns = ('specification', 'security') if state['classification']['risk'] == 'critical' else ('specification',)
            routing = self.router.route(Work(domain='planning', intent='plan',
                risk=state['classification']['risk'], concerns=concerns))
            skill_inputs = routing.required_inputs(self.router.catalog)
            state['routing'] = routing.as_dict()
            gate_errors = []
            if state['proposal']:
                gate_errors = self._gate(state['proposal'], state['source'], self._decisions(snapshot), True)['errors']
            context = bounded({'role': role, 'source': state['source'],
                'runtime_semantics': {'version': 3,
                    'milestone_dependencies': 'Factory closes prerequisite milestones before selecting ANY slice from a dependent milestone.',
                    'slice_dependencies': 'Slice dependencies reference slice IDs only; cross-milestone closure is enforced separately by the controller.',
                    'project_close': 'Runs only after all required milestones close; it cannot be a milestone closure prerequisite.',
                    'closure_evidence': 'requirement_acceptance.milestones are mandatory closed-receipt prerequisites in ProjectGate.obligations, not labels. MilestoneGate.publish requires every declared member slice to have a durable acceptance for the same sources. Slice and milestone dependencies are enforced before selection.',
                    'receipt_references': 'The final receipt binding.milestones references the immutable milestone receipts and commits; each milestone receipt.slices references its accepted slice execution IDs and commits. These prove Factory delivery order and closure. Product behavior still needs the declared product checks on the final candidate.',
                    'clean_copy_profile': 'For clean_copy checks and final project validation, Factory exports exact Git blobs and runs system Python with -I -S, an allowlist of stdlib/native runtime resources, no network, a read-only product workspace and fresh temporary /home and /tmp. Developer venv/site packages, host databases, untracked files, Factory state and inherited secrets are absent. The declared oracles and entrypoint run inside this same profile. Environment/procedure identities and code identity are bound to evidence.',
                    'clean_copy_limits': 'Successful checks in that profile establish independence from pre-existing application state or undeclared dependencies for the exercised paths. Temporary writes inside the fresh namespace are possible and disappear with it. This is not an audit proving absence of every filesystem/database operation or side effect on untested paths. Assess the authoritative condition as written; no dependence on residual state does not itself mean no temporary operations.'},
                'source_fingerprint': state['source_fingerprint'], 'proposal': state['proposal'],
                'review': state['review'], 'human_answers': self._decisions(snapshot),
                'gate_errors': gate_errors, 'skills': routing.context(),
                'limits': {**call_limits(state), 'detailed_slices': 3}})
            from .planning_binding import context as binding_context
            automatic = binding_context(self.store)
            if automatic:
                context['automatic_plan_binding'] = automatic
                bounded(context)
        except WorkflowError as exc:
            state['blockers'] = [str(exc)]
            self._mutate(snapshot, state, 'planning_preflight_failed')
            raise
        state['calls'] += 1
        state['critic_calls'] += int(role.startswith('critic'))
        state['blockers'] = []
        with self.store._connection(write=True) as db:
            self.store._check(db, snapshot['revision'])
            call_id = db.execute("INSERT INTO planning_calls(role,context,status) VALUES (?,?,'pending')",
                (role, canonical({'input': context, 'required_skills': skill_inputs}))).lastrowid
            self._save(db, state, snapshot['revision'], 'planning_call_started')
        revision = snapshot['revision'] + 1
        try:
            if self.model is None:
                from .codex_planning import CodexPlanning
                self.model = CodexPlanning()
            response = self.model.respond(context, skill_inputs=skill_inputs)
            self._accept_response(state, call_id, response, revision)
        except BaseException as exc:
            with self.store._connection(write=True) as db:
                current = db.execute('SELECT revision FROM workflow WHERE id=1').fetchone()[0]
                db.execute("UPDATE planning_calls SET error=?,status='failed' WHERE id=?", (str(exc), call_id))
                if current == revision:
                    saved = json.loads(db.execute('SELECT data FROM planning_run WHERE id=1').fetchone()[0])
                    saved['blockers'] = [str(exc) or type(exc).__name__]
                    self._save(db, saved, current, 'planning_call_failed')
                else:
                    self.store._record(db, current + 1, 'planning_stale_call', {'call_id': call_id})
            raise

    def _recover_completed_call(self, snapshot, state):
        """Finish an interrupted checkpoint from the existing analysis cache, even at its call limit."""
        from .analysis_execution import AnalysisModel
        if not isinstance(self.model, AnalysisModel):
            return False
        with self.store._connection() as db:
            row = db.execute('SELECT * FROM planning_calls ORDER BY id DESC LIMIT 1').fetchone()
        if not row or row['status'] not in ('failed', 'interrupted') or row['role'] != state['stage']:
            return False
        saved = json.loads(row['context'])
        context = saved['input']
        if (context['source'] != source_snapshot(snapshot) or context['proposal'] != state['proposal'] or
                context['human_answers'] != self._decisions(snapshot)):
            return False
        response = self.model.respond(context, skill_inputs=saved['required_skills'], _cached_only=True)
        if response is None:
            return False
        self._accept_response(state, row['id'], response, snapshot['revision'], recovered=True)
        return True

    def _accept_response(self, state, call_id, response, revision, *, recovered=False):
        role = state['stage']
        (validate_review if role.startswith('critic') else validate_plan)(response)
        bounded(response, MAX_OUTPUT_BYTES)
        with self.store._connection(write=True) as db:
            self.store._check(db, revision)
            db.execute("UPDATE planning_calls SET response=?,status='completed' WHERE id=?", (canonical(response), call_id))
            state['blockers'] = []
            if role.startswith('critic'):
                targets = {e['id'] for k in ('milestones', 'slices', 'gates', 'harness', 'risks') for e in state['proposal'][k]}
                targets.update(r['key'] for r in state['source']['requirements'])
                targets.update(e['id'] for k in SECTIONS for e in state['source']['baseline'][k])
                if state['proposal'].get('execution_binding'):
                    targets.add('execution_binding')
                    targets.update(c['id'] for c in state['proposal']['execution_binding']['checks'])
                # A critic may cite a deterministic diagnostic supplied for this
                # exact proposal. Use the saved call context, including on recovery;
                # do not accept arbitrary error names from the model response.
                row = db.execute('SELECT context FROM planning_calls WHERE id=?', (call_id,)).fetchone()
                diagnostics = json.loads(row['context'])['input'].get('gate_errors', [])
                targets.update(error.partition(':')[0] for error in diagnostics)
                findings = response['findings']
                if len({f['id'] for f in findings}) != len(findings) or any(not f['targets'] or not set(f['targets']) <= targets for f in findings):
                    raise WorkflowError('Planning findings require unique IDs and valid targets')
                state['review'] = response
                state['reviews'].append(response)
                if findings and not state['reconciliations']:
                    state.update(stage='reconcile', reconciliations=1)
                elif findings:
                    state.update(stage='blocked', blockers=['Unresolved review ' + f['id'] + ': ' + f['description'] for f in findings])
                else:
                    state['stage'] = 'gate'
            else:
                state['proposal'] = response
                for question in response['unresolved_questions']:
                    self._request(db, question)
                if not response['unresolved_questions']:
                    classification = classify(state['source'], response)
                    classification['required'] |= state['classification']['required']
                    state['classification'] = classification
                    state['stage'] = ('critic_final' if role == 'reconcile' else 'critic') if classification['required'] else 'gate'
            self._save(db, state, revision, 'planning_checkpoint_recovered' if recovered else 'planning_call_completed')

    def _complete(self, snapshot, state):
        with self.store._connection(write=True) as db:
            self.store._check(db, snapshot['revision'])
            review = {'classification': state['classification'], 'passes': state['reviews'],
                      'limits': call_limits(state)}
            append_revision(db, state['proposal'], state['source'], review, state['gate'], 'Initial progressive roadmap')
            state.update(stage='completed', blockers=[])
            db.execute("UPDATE workflow SET phase='execution' WHERE id=1")
            self._save(db, state, snapshot['revision'], 'planning_completed')
