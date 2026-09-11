"""Durable milestone runs and shared budgets above the existing slice journal."""
import json
import time
import uuid

from .architecture import canonical
from .registry import FactoryError

TERMINAL = ('checkpoint', 'milestone_closed', 'project_ready_for_validation', 'project_verified')


def migrate(db):
    for sql in (
        'CREATE TABLE IF NOT EXISTS continuations(id TEXT PRIMARY KEY, runtime_id TEXT NOT NULL, state TEXT NOT NULL, data TEXT NOT NULL, created_at REAL NOT NULL)',
        "CREATE UNIQUE INDEX IF NOT EXISTS continuation_single_open ON continuations((1)) WHERE state NOT IN ('checkpoint','milestone_ready')",
        'CREATE TABLE IF NOT EXISTS continuation_requests(id TEXT PRIMARY KEY, continuation_id TEXT NOT NULL REFERENCES continuations(id))',
        'CREATE TABLE IF NOT EXISTS continuation_calls(id TEXT PRIMARY KEY, continuation_id TEXT NOT NULL REFERENCES continuations(id), kind TEXT NOT NULL, unit_id TEXT NOT NULL, data TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS refinements(id TEXT PRIMARY KEY, input_key TEXT NOT NULL UNIQUE, state TEXT NOT NULL, data TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS refinement_revisions(id TEXT PRIMARY KEY, refinement_id TEXT NOT NULL, data TEXT NOT NULL)',
        "CREATE TRIGGER IF NOT EXISTS refinement_immutable BEFORE UPDATE ON refinement_revisions BEGIN SELECT RAISE(ABORT, 'Refinement revisions are immutable'); END",
        "CREATE TRIGGER IF NOT EXISTS refinement_no_delete BEFORE DELETE ON refinement_revisions BEGIN SELECT RAISE(ABORT, 'Refinement revisions are immutable'); END",
        'CREATE TABLE IF NOT EXISTS integrated_code(id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL)',
    ):
        db.execute(sql)
    db.execute('PRAGMA user_version=7')


def budget(db, identifier):
    row = db.execute('SELECT data FROM continuations WHERE id=?', (identifier,)).fetchone()
    data = json.loads(row[0])
    calls = [dict(r) for r in db.execute('SELECT * FROM continuation_calls WHERE continuation_id=?', (identifier,))]
    totals = {}
    unknown = 0
    rejected = 0
    for call in calls:
        record = json.loads(call['data'])
        usage = record.get('usage')
        if not usage:
            if record.get('request_rejection', {}).get('code') == 'invalid_json_schema':
                rejected += 1  # The request still consumes a call, with no model generation.
                continue
            unknown += 1
            continue
        # SDK total is cumulative within a thread, including repairs. Count each
        # distinct unit/thread once; a new slice starts its own context.
        key = (call['kind'], call['unit_id'], record.get('thread_id') or call['id'])
        totals[key] = max(totals.get(key, 0), usage.get('total', {}).get('totalTokens', 0))
    used = sum(totals.values())
    return {'calls': len(calls), 'tokens': used, 'usage_unknown_calls': unknown,
        'rejected_before_inference': rejected,
        'analysis_calls': sum(c['kind'] in ('discovery', 'architecture', 'planning') for c in calls),
        'analysis_tokens': sum(v for k, v in totals.items() if k[0] in ('discovery', 'architecture', 'planning')),
        'implementation_calls': sum(c['kind'] == 'implementation' for c in calls),
        'refinement_calls': sum(c['kind'] == 'refinement' for c in calls),
        'preparation_calls': sum(c['kind'] == 'preparation' for c in calls),
        'remediation_calls': sum(c['kind'] == 'remediation' for c in calls),
        'implementation_tokens': sum(v for k, v in totals.items() if k[0] == 'implementation'),
        'refinement_tokens': sum(v for k, v in totals.items() if k[0] == 'refinement'),
        'preparation_tokens': sum(v for k, v in totals.items() if k[0] == 'preparation'),
        'remediation_tokens': sum(v for k, v in totals.items() if k[0] == 'remediation'),
        'calls_remaining': max(0, data['limits']['max_calls'] - len(calls)),
        'tokens_remaining': max(0, data['limits']['max_tokens'] - used),
        'seconds_remaining': max(0, data['deadline'] - time.time())}


def check_budget(db, identifier, *, dispatch=False):
    value = budget(db, identifier)
    if value['seconds_remaining'] <= 0 or value['tokens_remaining'] <= 0 or (dispatch and value['calls_remaining'] <= 0):
        raise FactoryError('budget_exhausted', 'Persistent milestone run budget exhausted', details=value)
    return value


def reserve_call(db, data, attempt, kind='implementation'):
    identifier = data.get('continuation_id')
    if not identifier:
        return
    group = json.loads(db.execute('SELECT data FROM continuations WHERE id=?', (identifier,)).fetchone()[0])
    current = db.execute('SELECT * FROM factory_control WHERE id=1').fetchone()
    if group['runtime_id'] != data['run_id'] or current['run_id'] != data['run_id']:
        raise FactoryError('stale_run', 'Continuation ownership changed')
    if current['paused']:
        raise FactoryError('paused', 'Pause prevents dispatch')
    check_budget(db, identifier, dispatch=True)
    db.execute('INSERT INTO continuation_calls VALUES (?,?,?,?,?)',
               (attempt['id'], identifier, kind, data['id'], canonical({'usage': None})))


def observe_call(db, attempt):
    value = {'usage': attempt.get('usage'), 'thread_id': (attempt.get('runtime') or {}).get('thread_id')}
    db.execute('UPDATE continuation_calls SET data=? WHERE id=?', (canonical(value), attempt['id']))


class ContinuationStore:
    def __init__(self, store):
        self.store = store

    def latest(self):
        with self.store._connection() as db:
            if db.execute('PRAGMA user_version').fetchone()[0] < 7:
                return None
            row = db.execute('SELECT data FROM continuations ORDER BY created_at DESC, rowid DESC LIMIT 1').fetchone()
            return json.loads(row[0]) if row else None

    def save(self, data):
        with self.store._connection(write=True) as db:
            old = db.execute('SELECT runtime_id FROM continuations WHERE id=?', (data['id'],)).fetchone()[0]
            current = db.execute('SELECT run_id FROM factory_control WHERE id=1').fetchone()[0]
            if old != data['runtime_id'] or current != old:
                raise FactoryError('stale_run', 'Continuation owner was superseded')
            data['updated_at'] = time.time()
            db.execute('UPDATE continuations SET state=?,data=? WHERE id=?', (data['state'], canonical(data), data['id']))

    def inspect(self):
        data = self.latest()
        if data is None:
            return None
        with self.store._connection() as db:
            value = budget(db, data['id'])
        from .execution_store import ExecutionStore
        units = [a for a in ExecutionStore(self.store).acceptances() if a.get('continuation_id') == data['id']]
        value.update(work_units=len(units), remediations=sum(bool(a.get('remediation')) for a in units),
                     units_remaining=max(0, data['limits']['max_slices'] - len(units)))
        result = {**data, 'budget': value}
        if data.get('analysis'):
            from .runtime import Runtime, ACTIVE
            runtime = Runtime(self.store).state()
            result.update(phase=self.store.snapshot()['phase'], closed_milestones=[], next_milestone=None,
                          open_issues=[], verification_binding='pending accepted planning')
            if runtime['paused']:
                result['state'] = 'pause_requested' if runtime['status'] in ACTIVE else 'paused'
            elif runtime['status'] in ACTIVE:
                result['state'] = 'analysis_running'
            return result
        from .milestone_store import MilestoneStore
        from .milestone import eligible_milestone
        milestones = MilestoneStore(self.store)
        closed = milestones.closed(data['sources'])
        plan = self.store.snapshot()['planning']['roadmap']['plan']
        next_ = eligible_milestone(plan, closed) if plan else None
        result['closed_milestones'] = [{'milestone': k, 'receipt': r['id'], 'commit': r['commit']} for k, r in closed.items()]
        result['next_milestone'] = next_['id'] if next_ and next_['id'] != data['milestone'] else data.get('next_milestone')
        result['open_issues'] = [{k: i.get(k) for k in ('id', 'milestone', 'classification', 'state', 'attempts', 'execution_id')}
                                 for i in milestones.issues() if i['state'] != 'resolved']
        from .runtime import Runtime, ACTIVE
        runtime = Runtime(self.store).state()
        # A successor is a conditional candidate until active work is accepted.
        # Do not display the active slice itself as "next".
        if data.get('active_slice'):
            from .execution_store import ExecutionStore
            plan = self.store.snapshot()['planning']['roadmap']['plan']
            done = {a['slice_id'] for a in ExecutionStore(self.store).acceptances() if a['sources'] == data['sources']}
            done.add(data['active_slice'])
            candidates = [s for s in plan['slices'] if s['milestone'] == data['milestone'] and
                          s['id'] not in done and set(s['dependencies']) <= done]
            ready = [s for s in candidates if s['maturity'] == 'execution_ready' and s['id'] in plan['near_term']]
            result['next_slice'] = (ready or candidates)[0]['id'] if candidates else None
            result['next_slice_condition'] = 'Active slice acceptance, current sources, gates and remaining budget'
        if data['state'] not in TERMINAL:
            if runtime['paused']:
                result['state'] = 'pause_requested' if runtime['status'] in ACTIVE else 'paused'
            elif runtime['status'] == 'interrupted':
                result['state'] = 'interrupted'
            elif runtime['status'] == 'failed':
                result.update(state='infrastructure_failed', reason=runtime.get('error') or runtime.get('reason'))
            if data.get('execution_id'):
                from .execution_store import ExecutionStore
                execution = ExecutionStore(self.store).get(data['execution_id'])
                if not runtime['paused'] and runtime['status'] in ACTIVE and execution['state'] != 'checkpoint':
                    result['state'] = execution['state']
        return result
