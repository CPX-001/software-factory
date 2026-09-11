"""Bounded continuation of existing phases, independent of interface lifetime."""
from .runtime import Runtime

MAX_PHASE_STEPS = 4


def stop_reason(snapshot):
    # A discovery message may itself answer human decisions; let its worker interpret it.
    if snapshot['phase'] == 'discovery' and snapshot['discovery']['pending_turn'] is not None:
        return None
    if any(d['answer'] is None for d in snapshot['decisions']):
        return 'waiting_for_human'
    if snapshot['phase'] == 'discovery':
        return 'waiting_for_input'
    if snapshot['phase'] in ('architecture', 'planning'):
        return 'blocked' if snapshot[snapshot['phase']]['stage'] == 'blocked' else None
    if snapshot['phase'] == 'execution':
        return 'implementation_boundary'
    return 'completed' if snapshot['phase'] == 'completed' else 'unsupported_phase'


class Controller:
    def __init__(self, service):
        self.service = service

    def run(self, project, run_id):
        store = self.service._store(project)
        runtime = Runtime(store)
        with runtime.lock():
            with store._connection() as db:
                row = db.execute('SELECT status FROM factory_runs WHERE id=?', (run_id,)).fetchone()
                if not row or row['status'] != 'queued':
                    return  # Never replay an already claimed/finished run from another process.
            runtime.update(run_id, 'running')
            try:
                for _ in range(MAX_PHASE_STEPS):
                    if runtime.paused():
                        from .execution_store import ExecutionStore
                        execution = ExecutionStore(store).latest()
                        if execution and execution['run_id'] == run_id and execution['state'] != 'checkpoint':
                            execution.update(state='paused', reason='Paused before a new attempt')
                            ExecutionStore(store).save(execution)
                        runtime.update(run_id, 'paused', 'Pause saved at a worker checkpoint')
                        return
                    snapshot = store.snapshot()
                    if snapshot['phase'] == 'execution':
                        from .continuation import Continuation
                        continuation = Continuation(self.service, store)
                        group = continuation.journal.latest()
                        if group and group['runtime_id'] == run_id:
                            continuation.run(run_id)
                            return
                        from .execution import Execution
                        from .execution_store import ExecutionStore
                        execution = ExecutionStore(store).latest()
                        if execution and execution['run_id'] == run_id:
                            Execution(self.service, store).run(run_id)
                            return
                    reason = stop_reason(snapshot)
                    if reason:
                        runtime.update(run_id, reason, reason)
                        return
                    if snapshot['phase'] == 'discovery':
                        self.service._discovery(store).resume()
                    elif snapshot['phase'] == 'architecture':
                        self.service._architecture(store).run()
                    elif snapshot['phase'] == 'planning':
                        self.service._planning(store).run()
                    else:
                        raise AssertionError('Unimplemented phases must be stopped before dispatch')
                runtime.update(run_id, 'limit_reached', 'Bounded continuation limit reached')
            except BaseException as exc:
                runtime.update(run_id, 'failed', 'Worker stopped; resume explicitly to retry', str(exc)[:1000])
                raise
