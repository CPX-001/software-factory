"""Sequential accepted-code reference. Immutable receipts are publication intents.

On recovery a ref already advanced to the next receipt is reconciled into SQLite;
no implementation repeats and no reset/merge of the user's branch occurs.
"""
import json

from .architecture import canonical
from .execution_store import ExecutionStore
from .execution_workspace import git
from .registry import FactoryError

REF = 'refs/heads/factory/accepted'


def reconcile(store):
    journal = ExecutionStore(store)
    receipts = journal.acceptances()
    with store._connection() as db:
        row = db.execute('SELECT data FROM integrated_code WHERE id=1').fetchone()
    head = json.loads(row[0]) if row else None
    if head is None:
        head = {'commit': receipts[-1]['commit'] if receipts else git(store.project, 'rev-parse', 'HEAD'),
                'execution_id': receipts[-1]['execution_id'] if receipts else None, 'generation': len(receipts), 'ref': REF}
        ref = git(store.project, 'for-each-ref', '--format=%(objectname)', REF)
        if ref and ref != head['commit']:
            raise FactoryError('integration_conflict', 'Existing managed accepted ref has an unexpected commit')
        if not ref:
            git(store.project, 'update-ref', REF, head['commit'], '0' * len(head['commit']))
        with store._connection(write=True) as db:
            db.execute('INSERT INTO integrated_code VALUES (1,?)', (canonical(head),))
    after = head['execution_id'] is None
    pending = []
    for receipt in receipts:
        if after:
            pending.append(receipt)
        elif receipt['execution_id'] == head['execution_id']:
            after = True
    for receipt in pending:
        execution = journal.get(receipt['execution_id'])
        if execution['base_commit'] != head['commit']:
            raise FactoryError('integration_conflict', 'Accepted slice does not descend from the current integrated base')
        current = git(store.project, 'rev-parse', REF)
        if current == head['commit']:
            git(store.project, 'update-ref', REF, receipt['commit'], head['commit'])
        elif current != receipt['commit']:
            raise FactoryError('integration_conflict', 'A newer/unexpected accepted reference cannot be overwritten')
        next_ = {'commit': receipt['commit'], 'execution_id': receipt['execution_id'],
                 'generation': head['generation'] + 1, 'ref': REF}
        with store._connection(write=True) as db:
            old = json.loads(db.execute('SELECT data FROM integrated_code WHERE id=1').fetchone()[0])
            if old != head:
                raise FactoryError('integration_conflict', 'Integrated publication ownership changed')
            db.execute('UPDATE integrated_code SET data=? WHERE id=1', (canonical(next_),))
        head = next_
    if git(store.project, 'rev-parse', REF) != head['commit']:
        raise FactoryError('integration_conflict', 'Managed accepted reference differs from its durable receipt')
    return head
