"""Versioned integrated validation, immutable closure receipts and persistent issues."""
import json
import time

from .architecture import canonical, fingerprint
from .registry import FactoryError


def migrate(db):
    first_upgrade = not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='milestone_validations'").fetchone()
    if first_upgrade:
        # Step 9 ended the RUN at milestone_ready, without closing the milestone.
        # Preserve its exhausted limits/history and allow explicit authorization of
        # a new run for closure. New transient milestone_ready is always recoverable.
        for row in db.execute("SELECT id,data FROM continuations WHERE state='milestone_ready'").fetchall():
            data = json.loads(row['data'])
            data.update(state='checkpoint', legacy_stop='milestone_ready',
                        reason='Previous run ended with integrated milestone validation pending; explicitly authorize its closure run')
            db.execute("UPDATE continuations SET state='checkpoint',data=? WHERE id=?", (canonical(data), row['id']))
    for sql in (
        'CREATE TABLE IF NOT EXISTS milestone_validations(id TEXT PRIMARY KEY, milestone TEXT NOT NULL, data TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS milestone_acceptances(id TEXT PRIMARY KEY, milestone TEXT NOT NULL, source_key TEXT NOT NULL, data TEXT NOT NULL, UNIQUE(milestone, source_key))',
        'CREATE TABLE IF NOT EXISTS milestone_issues(id TEXT PRIMARY KEY, milestone TEXT NOT NULL, data TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS milestone_reviews(validation_id TEXT NOT NULL, check_id TEXT NOT NULL, decision_id INTEGER NOT NULL REFERENCES decisions(id), PRIMARY KEY(validation_id, check_id))',
        "CREATE TRIGGER IF NOT EXISTS milestone_acceptance_immutable BEFORE UPDATE ON milestone_acceptances BEGIN SELECT RAISE(ABORT, 'Milestone receipts are immutable'); END",
        "CREATE TRIGGER IF NOT EXISTS milestone_acceptance_no_delete BEFORE DELETE ON milestone_acceptances BEGIN SELECT RAISE(ABORT, 'Milestone receipts are immutable'); END",
    ):
        db.execute(sql)
    db.execute('DROP INDEX IF EXISTS continuation_single_open')
    db.execute("CREATE UNIQUE INDEX continuation_single_open ON continuations((1)) WHERE state NOT IN ('checkpoint','milestone_closed','project_ready_for_validation')")
    db.execute('PRAGMA user_version=8')


def fence(db, group, *, allow_paused=False):
    control = db.execute('SELECT * FROM factory_control WHERE id=1').fetchone()
    row = db.execute('SELECT runtime_id FROM continuations WHERE id=?', (group['id'],)).fetchone()
    if not row or row[0] != group['runtime_id'] or control['run_id'] != group['runtime_id']:
        raise FactoryError('stale_run', 'Milestone runtime was superseded')
    if control['paused'] and not allow_paused:
        raise FactoryError('paused', 'Pause prevents milestone work/publication')


class MilestoneStore:
    def __init__(self, store):
        self.store = store

    def rows(self, table):
        assert table in ('milestone_acceptances', 'milestone_validations', 'milestone_issues')
        with self.store._connection() as db:
            if db.execute('PRAGMA user_version').fetchone()[0] < 8:
                return []
            return [json.loads(r[0]) for r in db.execute('SELECT data FROM ' + table + ' ORDER BY rowid')]

    def receipts(self, sources=None):
        return [r for r in self.rows('milestone_acceptances') if sources is None or r['sources'] == sources]

    def closed(self, sources):
        from .execution_store import ExecutionStore
        definition_id = ExecutionStore(self.store).policy().get('definition_id')
        return {r['milestone']: r for r in self.receipts(sources) if r['definition_id'] == definition_id}

    def save_validation(self, group, data):
        with self.store._connection(write=True) as db:
            fence(db, group, allow_paused=True)
            db.execute('INSERT INTO milestone_validations VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                       (data['id'], data['milestone'], canonical(data)))

    def issues(self, milestone=None):
        return [r for r in self.rows('milestone_issues') if milestone is None or r['milestone'] == milestone]

    def save_issue(self, group, issue):
        with self.store._connection(write=True) as db:
            fence(db, group)
            db.execute('INSERT INTO milestone_issues VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                       (issue['id'], issue['milestone'], canonical(issue)))

    def progress(self, snapshot):
        from .execution import current_sources
        try:
            sources = current_sources(snapshot)
        except FactoryError:
            sources = None
        receipts = self.receipts()
        closed = self.closed(sources) if sources else {}
        plan = snapshot['planning']['roadmap']['plan'] if snapshot['planning']['roadmap'] else None
        if not plan:
            return {'milestones': [], 'receipts': receipts, 'requirements': []}
        requirements = []
        for coverage in plan['coverage']:
            key = coverage['requirement']
            contributors = [m['id'] for m in plan['milestones'] if key in m['requirements']]
            contributions = [{'milestone': m, 'receipt': closed[m]['id'] if m in closed else None,
                              'status': 'verified_contribution' if m in closed and coverage['disposition'] == 'covered' else 'pending'}
                             for m in contributors]
            acceptance = next((a for r in closed.values() for a in r.get('requirement_acceptances', []) if a['requirement'] == key), None)
            requirements.append({'requirement': key, 'owner': coverage['milestone'], 'disposition': coverage['disposition'],
                'status': 'satisfied' if acceptance else 'active' if coverage['disposition'] == 'covered' else coverage['disposition'],
                'contributions': contributions, 'acceptance': acceptance})
        return {'milestones': [{**m, 'planned_status': m['status'], 'status': 'closed' if m['id'] in closed else 'open',
                 'receipt': closed[m['id']]['id'] if m['id'] in closed else None} for m in plan['milestones']],
                'receipts': [{**r, 'current_sources': r['id'] in {c['id'] for c in closed.values()},
                              'scope': 'Evidence for the recorded commit and sources only'} for r in receipts],
                'requirements': requirements}
