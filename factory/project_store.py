"""Project records use the closure journal and the same process/publication fence."""
import json
from pathlib import Path

from .milestone_store import MilestoneStore


def migrate(db):
    for sql in (
        'CREATE TABLE IF NOT EXISTS project_validations(id TEXT PRIMARY KEY, milestone TEXT NOT NULL, data TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS project_acceptances(id TEXT PRIMARY KEY, milestone TEXT NOT NULL, source_key TEXT NOT NULL, data TEXT NOT NULL, UNIQUE(milestone, source_key))',
        'CREATE TABLE IF NOT EXISTS project_issues(id TEXT PRIMARY KEY, milestone TEXT NOT NULL, data TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS project_reviews(validation_id TEXT NOT NULL, check_id TEXT NOT NULL, decision_id INTEGER NOT NULL REFERENCES decisions(id), PRIMARY KEY(validation_id, check_id))',
        'CREATE TABLE IF NOT EXISTS project_contracts(id TEXT PRIMARY KEY, data TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS project_remediation(id INTEGER PRIMARY KEY CHECK(id=1), validation_id TEXT NOT NULL, issue_ids TEXT NOT NULL)',
    ):
        db.execute(sql)
    for table in ('project_acceptances', 'project_contracts', 'project_remediation'):
        for action in ('UPDATE', 'DELETE'):
            db.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT, 'Project history and global budget are immutable'); END")
    db.execute('DROP INDEX IF EXISTS continuation_single_open')
    db.execute("CREATE UNIQUE INDEX continuation_single_open ON continuations((1)) WHERE state NOT IN ('checkpoint','milestone_closed','project_ready_for_validation','project_verified')")
    db.execute('PRAGMA user_version=9')


class ProjectStore(MilestoneStore):
    def inspect(self, *, full=False):
        from .execution import current_sources
        from .execution_store import ExecutionStore
        from .execution_workspace import git
        from .integrated_code import REF
        from .continuation_store import ContinuationStore
        from .registry import FactoryError
        snapshot = self.store.snapshot()
        group = ContinuationStore(self.store).latest() or {}
        receipts = self.receipts()
        receipt = receipts[-1] if receipts else None
        current = False
        with self.store._connection() as db:
            review_ids = ({r[0] for table in ('milestone_reviews', 'project_reviews')
                           for r in db.execute('SELECT decision_id FROM ' + table)}
                          if db.execute('PRAGMA user_version').fetchone()[0] >= 9 else set())
        applicable_decisions = [d for d in snapshot['decisions'] if d['id'] not in review_ids]
        try:
            current = bool(receipt and receipt['sources'] == current_sources(snapshot) and
                           receipt['definition_id'] == ExecutionStore(self.store).policy().get('definition_id') and
                           receipt['binding']['decisions'] == applicable_decisions and
                           receipt['binding']['adrs'] == snapshot['architecture']['adrs'] and
                           not snapshot['architecture']['blockers'] and not snapshot['planning']['blockers'] and
                           not any(d['answer'] is None for d in snapshot['decisions']) and
                           receipt['commit'] == git(self.store.project, 'rev-parse', REF))
        except FactoryError:
            pass
        validations = self.rows('milestone_validations')
        latest = validations[-1] if validations else None
        project_active = bool(group.get('final_authorization'))
        state = ('project_verified' if current else 'version_pending' if receipt else
                 group.get('state', 'not_started') if project_active else
                 'project_ready_for_validation' if group.get('state') == 'project_ready_for_validation' else 'milestones_pending')
        result = {'state': state, 'candidate_commit': latest['binding']['commit'] if latest else group.get('candidate_commit') if project_active else None,
            'validated_version': {'commit': receipt['commit'], 'receipt': receipt['id'], 'current': current} if receipt else None,
            'contract_id': latest['binding']['contract_id'] if latest else group.get('project_contract'),
            'pending_criteria': ((group.get('pending_criteria', []) +
                                 [c for c in latest.get('criteria', []) if c['status'] != 'PASS'] if latest else
                                 group.get('pending_criteria', [])) if project_active else []),
            'checks': [{k: e.get(k) for k in ('check_id', 'status', 'reason', 'integration_mode')}
                       for e in latest['evidence']] if latest else [],
            'blocker': group.get('diagnostic') if project_active and not current else None,
            'remediation': group.get('remediation') if project_active else None,
            'delivery': str(self.store.path.parent / 'deliveries' / receipt['id'] / 'REPORT.md') if receipt else None,
            'exclusions': [c for c in snapshot['planning']['roadmap']['plan']['coverage']
                           if c['disposition'] in ('deferred', 'out_of_scope')] if snapshot['planning']['roadmap'] else []}
        if result['delivery']:
            result['delivery_ready'] = Path(result['delivery']).is_file()
            if not result['delivery_ready']:
                result['state'] = 'delivery_pending' if current else state
                result['blocker'] = group.get('diagnostic') or {'code': 'delivery_pending', 'next_step': 'Resume to recover the report from the durable receipt'}
        if full:
            result.update(receipts=receipts, validations=validations, issues=self.issues())
        return result

    def rows(self, table):
        assert table in ('milestone_acceptances', 'milestone_validations', 'milestone_issues')
        with self.store._connection() as db:
            if db.execute('PRAGMA user_version').fetchone()[0] < 9:
                return []
            return [json.loads(r[0]) for r in db.execute('SELECT data FROM ' + table.replace('milestone_', 'project_') + ' ORDER BY rowid')]

    def save_validation(self, group, data):
        self._save(group, data, 'project_validations')

    def save_issue(self, group, data):
        self._save(group, data, 'project_issues')

    def _save(self, group, data, table):
        from .architecture import canonical
        from .milestone_store import fence
        with self.store._connection(write=True) as db:
            fence(db, group, allow_paused=table == 'project_validations')
            db.execute('INSERT INTO ' + table + ' VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                       (data['id'], data['milestone'], canonical(data)))
