"""Durable run intent and process locks, independent of workflow revisions."""
from contextlib import contextmanager
import fcntl
import json
import time
import uuid

from .registry import FactoryError

ACTIVE = ('queued', 'running', 'pause_requested')


def migrate(db):
    db.execute('''CREATE TABLE IF NOT EXISTS factory_control (
        id INTEGER PRIMARY KEY CHECK(id=1), paused INTEGER NOT NULL DEFAULT 0, run_id TEXT)''')
    db.execute('INSERT OR IGNORE INTO factory_control(id) VALUES (1)')
    db.execute('''CREATE TABLE IF NOT EXISTS factory_runs (
        id TEXT PRIMARY KEY, status TEXT NOT NULL, reason TEXT, error TEXT,
        created_at REAL NOT NULL, updated_at REAL NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS factory_control_events (
        id INTEGER PRIMARY KEY, kind TEXT NOT NULL, data TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))''')
    db.execute('''CREATE TABLE IF NOT EXISTS factory_requests (
        id TEXT PRIMARY KEY, message TEXT NOT NULL, turn_id INTEGER NOT NULL REFERENCES discovery_turns(id))''')
    if db.execute('PRAGMA user_version').fetchone()[0] < 4:
        db.execute('PRAGMA user_version=4')


def read_state(db):
    default = {'paused': False, 'run_id': None, 'status': 'idle', 'reason': None, 'error': None}
    if db.execute('PRAGMA user_version').fetchone()[0] < 4:
        return default
    control = db.execute('SELECT * FROM factory_control WHERE id=1').fetchone()
    run = db.execute('SELECT * FROM factory_runs WHERE id=?', (control['run_id'],)).fetchone()
    return {**default, **(dict(run) if run else {}), 'paused': bool(control['paused']), 'run_id': control['run_id']}


class Runtime:
    def __init__(self, store):
        self.store = store

    @contextmanager
    def lock(self, name='run', *, timeout=0):
        with (self.store.path.parent / (name + '.lock')).open('a') as handle:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise FactoryError('run_busy', 'Factory is already working; consult status before sending another input') from exc
                    time.sleep(.01)  # Only short admission may wait, never the worker lock.
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def live(self):
        try:
            with self.lock():
                return False
        except FactoryError:
            return True

    def state(self):
        with self.store._connection() as db:
            state = read_state(db)
        if state['status'] in ACTIVE and not self.live():
            # Launching has a short grace window; no PID reuse assumptions or signal sending.
            if state['status'] != 'queued' or time.time() - state.get('updated_at', 0) > 30:
                state.update(status='interrupted', reason='Worker exited before its final checkpoint; resume explicitly')
        return state

    def paused(self):
        with self.store._connection() as db:
            return read_state(db)['paused']

    @staticmethod
    def event(db, kind, data):
        db.execute('INSERT INTO factory_control_events(kind,data) VALUES (?, ?)', (kind, json.dumps(data)))

    def pause(self):
        with self.store._connection(write=True) as db:
            db.execute('UPDATE factory_control SET paused=1 WHERE id=1')
            db.execute("UPDATE factory_runs SET status='pause_requested', updated_at=? WHERE id=(SELECT run_id FROM factory_control) AND status='running'", (time.time(),))
            self.event(db, 'pause_requested', {})

    def unpause(self):
        with self.store._connection(write=True) as db:
            db.execute('UPDATE factory_control SET paused=0 WHERE id=1')
            self.event(db, 'resume_requested', {})

    def queue(self):
        identifier = str(uuid.uuid4())
        with self.store._connection(write=True) as db:
            previous = read_state(db)
            if previous['run_id']:
                db.execute("UPDATE factory_runs SET status='interrupted', reason='Superseded by explicit recovery' WHERE id=? AND status IN ('running','queued','pause_requested')", (previous['run_id'],))
            now = time.time()
            db.execute('INSERT INTO factory_runs VALUES (?, ?, NULL, NULL, ?, ?)', (identifier, 'queued', now, now))
            db.execute('UPDATE factory_control SET run_id=? WHERE id=1', (identifier,))
            self.event(db, 'run_queued', {'run_id': identifier})
        return identifier

    def update(self, identifier, status, reason=None, error=None):
        with self.store._connection(write=True) as db:
            current = read_state(db)
            if current['run_id'] != identifier:
                raise FactoryError('stale_run', 'A newer run superseded this worker')
            db.execute('UPDATE factory_runs SET status=?,reason=?,error=?,updated_at=? WHERE id=?',
                       (status, reason, error, time.time(), identifier))
            self.event(db, 'run_' + status, {'run_id': identifier, 'reason': reason})
