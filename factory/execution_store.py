"""Short, fenced journal transitions. Git publication has a separate durable intent."""
import json
import time
import uuid
import fcntl

from .architecture import canonical
from .execution_contract import DEFAULT_POLICY
from .registry import FactoryError

OPEN = ('queued', 'preflight', 'implementing', 'repairing', 'applying', 'verifying', 'publishing',
        'pause_requested', 'paused', 'interrupted', 'blocked', 'waiting_decision', 'quota_blocked',
        'validation_pending', 'infrastructure_failed', 'failed', 'budget_exhausted')


def migrate(db):
    for sql in (
        '''CREATE TABLE IF NOT EXISTS execution_policy(id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS execution_definitions(
            id TEXT PRIMARY KEY, data TEXT NOT NULL, created_at REAL NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS executions(
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL, state TEXT NOT NULL, data TEXT NOT NULL,
            created_at REAL NOT NULL, updated_at REAL NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS execution_attempts(
            id TEXT PRIMARY KEY, execution_id TEXT NOT NULL REFERENCES executions(id),
            ordinal INTEGER NOT NULL, token TEXT NOT NULL, data TEXT NOT NULL,
            UNIQUE(execution_id, ordinal))''',
        '''CREATE TABLE IF NOT EXISTS execution_requests(
            id TEXT PRIMARY KEY, execution_id TEXT NOT NULL REFERENCES executions(id))''',
        '''CREATE TABLE IF NOT EXISTS execution_events(
            id INTEGER PRIMARY KEY, execution_id TEXT NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL,
            created_at REAL NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS execution_acceptances(
            execution_id TEXT PRIMARY KEY REFERENCES executions(id), slice_id TEXT NOT NULL,
            plan_revision INTEGER NOT NULL, data TEXT NOT NULL, UNIQUE(slice_id, plan_revision))''',
        '''CREATE UNIQUE INDEX IF NOT EXISTS execution_single_open ON executions((1)) WHERE state != 'checkpoint' ''',
        '''CREATE TRIGGER IF NOT EXISTS definition_no_update BEFORE UPDATE ON execution_definitions
           BEGIN SELECT RAISE(ABORT, 'Verification definitions are immutable'); END''',
        '''CREATE TRIGGER IF NOT EXISTS definition_no_delete BEFORE DELETE ON execution_definitions
           BEGIN SELECT RAISE(ABORT, 'Verification definitions are immutable'); END''',
        '''CREATE TRIGGER IF NOT EXISTS acceptance_no_update BEFORE UPDATE ON execution_acceptances
           BEGIN SELECT RAISE(ABORT, 'Acceptances are immutable'); END''',
        '''CREATE TRIGGER IF NOT EXISTS acceptance_no_delete BEFORE DELETE ON execution_acceptances
           BEGIN SELECT RAISE(ABORT, 'Acceptances are immutable'); END''',
    ):
        db.execute(sql)
    db.execute('INSERT OR IGNORE INTO execution_policy VALUES (1, ?)', (canonical(DEFAULT_POLICY),))
    db.execute('PRAGMA user_version=6')


class ExecutionStore:
    def __init__(self, store):
        self.store = store

    def policy(self):
        with self.store._connection() as db:
            if db.execute('PRAGMA user_version').fetchone()[0] < 6:
                return dict(DEFAULT_POLICY)
            return json.loads(db.execute('SELECT data FROM execution_policy WHERE id=1').fetchone()[0])

    def latest(self):
        with self.store._connection() as db:
            if db.execute('PRAGMA user_version').fetchone()[0] < 6:
                return None
            row = db.execute('SELECT data FROM executions ORDER BY created_at DESC, id DESC LIMIT 1').fetchone()
            return json.loads(row[0]) if row else None

    def get(self, identifier):
        with self.store._connection() as db:
            row = db.execute('SELECT data FROM executions WHERE id=?', (identifier,)).fetchone()
            if not row:
                raise FactoryError('execution_missing', 'Execution does not exist')
            return json.loads(row[0])

    def acceptances(self):
        with self.store._connection() as db:
            if db.execute('PRAGMA user_version').fetchone()[0] < 6:
                return []
            return [json.loads(r[0]) for r in db.execute('SELECT data FROM execution_acceptances ORDER BY rowid')]

    def definition(self, identifier):
        with self.store._connection() as db:
            row = db.execute('SELECT data FROM execution_definitions WHERE id=?', (identifier,)).fetchone()
            return json.loads(row[0]) if row else None

    def attempts(self, identifier):
        with self.store._connection() as db:
            return [json.loads(r[0]) for r in db.execute(
                'SELECT data FROM execution_attempts WHERE execution_id=? ORDER BY ordinal', (identifier,))]

    @staticmethod
    def event(db, identifier, kind, data=None):
        db.execute('INSERT INTO execution_events(execution_id,kind,data,created_at) VALUES (?,?,?,?)',
                   (identifier, kind, canonical(data or {}), time.time()))

    def save(self, data, *, token=None, kind=None):
        """Only the owning runtime and current attempt may publish a transition."""
        with self.store._connection(write=True) as db:
            row = db.execute('SELECT data FROM executions WHERE id=?', (data['id'],)).fetchone()
            old = json.loads(row[0])
            current_run = db.execute('SELECT run_id FROM factory_control WHERE id=1').fetchone()[0]
            if old['run_id'] != data['run_id'] or current_run != data['run_id'] or old['state'] == 'checkpoint':
                raise FactoryError('stale_execution', 'Execution ownership changed; old worker cannot publish')
            if token is not None and old.get('attempt_token') != token:
                raise FactoryError('stale_attempt', 'A newer attempt superseded this worker')
            if old.get('attempt_token') != data.get('attempt_token'):
                raise FactoryError('stale_attempt', 'Attempt ownership changed before this transition')
            data['updated_at'] = time.time()
            data['last_event'] = kind or data['state']
            db.execute('UPDATE executions SET state=?,data=?,updated_at=? WHERE id=?',
                (data['state'], canonical(data), data['updated_at'], data['id']))
            self.event(db, data['id'], data['last_event'])

    def start_attempt(self, data):
        with self.store._connection(write=True) as db:
            old = json.loads(db.execute('SELECT data FROM executions WHERE id=?', (data['id'],)).fetchone()[0])
            current = db.execute('SELECT run_id FROM factory_control WHERE id=1').fetchone()[0]
            if old['run_id'] != data['run_id'] or current != data['run_id']:
                raise FactoryError('stale_execution', 'Runtime ownership changed')
            count = db.execute('SELECT count(*) FROM execution_attempts WHERE execution_id=?', (data['id'],)).fetchone()[0]
            if count >= data['policy']['max_attempts']:
                raise FactoryError('budget_exhausted', 'Persistent implementation/repair attempt budget exhausted')
            if db.execute('SELECT paused FROM factory_control WHERE id=1').fetchone()[0]:
                raise FactoryError('paused', 'Pause prevents new attempts')
            token = str(uuid.uuid4())
            attempt = {'id': str(uuid.uuid4()), 'ordinal': count + 1, 'token': token,
                'kind': 'implementation' if count == 0 else 'repair', 'state': 'started',
                'started_at': time.time(), 'completed_at': None, 'runtime': None, 'response': None,
                'worker_status': None, 'verification_status': None, 'usage': None}
            from .continuation_store import reserve_call
            reserve_call(db, data, attempt, 'remediation' if data.get('remediation') else 'implementation')
            data.update(attempt_token=token, attempt=count + 1,
                        state='implementing' if count == 0 else 'repairing', updated_at=time.time())
            db.execute('INSERT INTO execution_attempts VALUES (?,?,?,?,?)',
                (attempt['id'], data['id'], count + 1, token, canonical(attempt)))
            db.execute('UPDATE executions SET state=?,data=?,updated_at=? WHERE id=?',
                (data['state'], canonical(data), data['updated_at'], data['id']))
            self.event(db, data['id'], data['state'], {'attempt': count + 1})
            return attempt

    def save_attempt(self, data, attempt):
        with self.store._connection(write=True) as db:
            old = json.loads(db.execute('SELECT data FROM executions WHERE id=?', (data['id'],)).fetchone()[0])
            current = db.execute('SELECT run_id FROM factory_control WHERE id=1').fetchone()[0]
            if (old.get('attempt_token') != attempt['token'] or old['run_id'] != data['run_id']
                    or current != data['run_id'] or old['state'] == 'checkpoint'):
                raise FactoryError('stale_attempt', 'Worker result belongs to a superseded attempt')
            db.execute('UPDATE execution_attempts SET data=? WHERE id=? AND token=?',
                (canonical(attempt), attempt['id'], attempt['token']))
            from .continuation_store import observe_call
            observe_call(db, attempt)

    def inspect(self, *, full=False):
        data = self.latest()
        if not data:
            policy = self.policy()
            return {'enabled': policy['enabled'], 'state': 'not_started', **({'policy': policy,
                'definition': self.definition(policy['definition_id']) if policy.get('definition_id') else None} if full else {})}
        fields = ('id', 'run_id', 'slice_id', 'state', 'attempt', 'last_event', 'reason',
                  'worktree', 'branch', 'commit', 'usage', 'quota', 'created_at', 'updated_at', 'deadline',
                  'verification', 'blockers', 'definition_id', 'diagnostic', 'runtime_info', 'quota_before')
        result = {k: data.get(k) for k in fields}
        result['enabled'] = self.policy()['enabled']
        lease = self.store.path.parent / 'executions' / data['id'] / 'process.lock'
        live = False
        if lease.is_file():
            with lease.open('r') as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    live = True
        result['sandbox_alive'] = live
        if full:
            result = {**data, 'attempts': self.attempts(data['id']),
                      'authorization': self.policy(),
                      'definition': self.definition(data['definition_id']), 'sandbox_alive': live,
                      'harness_definition': self.definition(data['harness_revision']) if data.get('harness_revision') else None}
        else:
            result['verification'] = [{k: e.get(k) for k in ('check_id', 'status', 'reason', 'criteria', 'code_id')}
                                      for e in data.get('verification', [])]
        from .runtime import Runtime
        runtime = Runtime(self.store).state()
        if runtime['paused'] and data['state'] != 'checkpoint':
            result.update(recorded_state=data['state'], state='pause_requested' if live or runtime['status'] in ('running', 'pause_requested') else 'paused')
        elif runtime['status'] == 'interrupted' and data['state'] in ('queued', 'preflight', 'implementing', 'repairing', 'applying', 'verifying', 'publishing'):
            result.update(recorded_state=data['state'], state='interrupted',
                          reason='Sandbox is still alive; no duplicate will launch' if live else 'Controller ended; resume inspects saved attempt and worktree')
        return result
