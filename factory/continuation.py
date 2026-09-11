"""Deterministic sequential execution, integrated closure and authorized continuity."""
import json
import time
import uuid

from .architecture import canonical
from .continuation_store import ContinuationStore, TERMINAL, check_budget
from .execution import Execution, current_sources
from .execution_store import ExecutionStore
from .execution_sandbox import LinuxSandbox
from .integrated_code import reconcile
from .registry import FactoryError
from .runtime import Runtime, ACTIVE
from .verification import capability_errors, check_coverage, gates_for
from .milestone_store import MilestoneStore
from .milestone import MilestoneGate, eligible_milestone


class Continuation:
    def __init__(self, service, store):
        self.service, self.store = service, store
        self.journal = ContinuationStore(store)
        self.executions = ExecutionStore(store)
        self.runtime = Runtime(store)

    def start(self, request_id):
        with self.runtime.lock('launch', timeout=2):
            with self.store._connection() as db:
                prior = db.execute('SELECT continuation_id FROM continuation_requests WHERE id=?', (request_id,)).fetchone()
            data = self.journal.latest()
            if prior:
                return prior[0]
            if data and data['state'] not in TERMINAL:
                with self.store._connection(write=True) as db:
                    db.execute('INSERT INTO continuation_requests VALUES (?,?)', (request_id, data['id']))
                return data['id']
            old = self.executions.latest()
            if self.runtime.live() or self.runtime.state()['status'] in ACTIVE or (old and old['state'] != 'checkpoint'):
                raise FactoryError('run_busy', 'Recover the existing worker before starting a milestone run')
            snapshot, sources, _, policy = Execution(self.service, self.store).prerequisites(check_selected=False)
            limits = policy.get('continuation', {})
            if not limits.get('enabled'):
                raise FactoryError('continuation_disabled', 'Explicitly authorize multislice execution first')
            closed = MilestoneStore(self.store).closed(sources)
            milestone = eligible_milestone(snapshot['planning']['roadmap']['plan'], closed)
            if not milestone:
                if data and data['state'] == 'project_ready_for_validation' and data['sources'] == sources:
                    with self.store._connection(write=True) as db:
                        db.execute('INSERT INTO continuation_requests VALUES (?,?)', (request_id, data['id']))
                    return data['id']
                raise FactoryError('milestone_gate_pending', 'No active milestone with validated prerequisites is available')
            if data and data['state'] == 'milestone_ready' and data['sources'] == sources and data['milestone'] == milestone['id']:
                with self.store._connection(write=True) as db:
                    db.execute('INSERT INTO continuation_requests VALUES (?,?)', (request_id, data['id']))
                return data['id']  # A reconnect cannot create another empty equivalent run.
            head = reconcile(self.store)
            runtime_id = self.runtime.queue()
            now = time.time()
            data = {'id': str(uuid.uuid4()), 'runtime_id': runtime_id, 'state': 'queued',
                'milestone': milestone['id'], 'sources': sources, 'policy': policy,
                'limits': limits, 'created_at': now, 'updated_at': now, 'deadline': now + limits['max_seconds'],
                'accepted': [], 'execution_id': None, 'refinement_id': None, 'active_slice': None,
                'next_slice': None, 'reason': None, 'diagnostic': None, 'integrated': head}
            with self.store._connection(write=True) as db:
                db.execute('INSERT INTO continuations VALUES (?,?,?,?,?)',
                           (data['id'], runtime_id, 'queued', canonical(data), now))
                db.execute('INSERT INTO continuation_requests VALUES (?,?)', (request_id, data['id']))
            self.service._launch_registered(self.project_id(), runtime_id, self.runtime)
            return data['id']

    def project_id(self):
        return next(p['id'] for p in self.service.registry.projects() if p['path'] == str(self.store.project))

    def stop(self, data, state, reason, details=None):
        data.update(state=state, reason=reason, diagnostic=details)
        self.journal.save(data)
        self.runtime.update(data['runtime_id'], state, reason)

    def pause(self, data):
        if data.get('validation_id'):
            place = self.store.path.parent / 'milestones' / data['validation_id'] / 'runtime'
            place.mkdir(parents=True, exist_ok=True)
            (place / 'pause').touch()
        if data.get('refinement_id'):
            place = self.store.path.parent / 'refinements' / data['refinement_id'] / 'runtime'
            place.mkdir(parents=True, exist_ok=True)
            (place / 'pause').touch()

    def resume(self):
        with self.runtime.lock('launch'):
            self.runtime.unpause()
            data = self.journal.latest()
            if not data or data['state'] in TERMINAL:
                return
            if self.runtime.live() or self.runtime.state()['status'] in ACTIVE:
                return
            if any(d['answer'] is None for d in self.store.snapshot()['decisions']):
                return
            guards = []
            if data.get('validation_id'):
                guards.append(self.store.path.parent / 'milestones' / data['validation_id'] / 'runtime')
            if data.get('refinement_id'):
                guards.append(self.store.path.parent / 'refinements' / data['refinement_id'] / 'runtime')
            execution = self.executions.latest()
            if execution and execution.get('continuation_id') == data['id']:
                guards.append(self.store.path.parent / 'executions' / execution['id'])
            for guard in guards:
                sandbox = LinuxSandbox(guard)
                if sandbox.live():
                    return
                (guard / 'pause').unlink(missing_ok=True)
            # New process fence, SAME logical run, deadline and attempt/call journals.
            runtime_id = self.runtime.queue()
            data['previous_stop'] = {k: data.get(k) for k in ('state', 'reason', 'diagnostic', 'updated_at')}
            data.update(runtime_id=runtime_id, state='queued', reason=None, diagnostic=None)
            with self.store._connection(write=True) as db:
                db.execute('UPDATE continuations SET runtime_id=?,state=?,data=? WHERE id=?',
                           (runtime_id, 'queued', canonical(data), data['id']))
                if execution and execution.get('continuation_id') == data['id'] and execution['state'] != 'checkpoint':
                    execution.update(run_id=runtime_id, state='queued')
                    db.execute('UPDATE executions SET run_id=?,state=?,data=? WHERE id=?',
                               (runtime_id, 'queued', canonical(execution), execution['id']))
            self.service._launch_registered(self.project_id(), runtime_id, self.runtime)

    def choose(self, data, snapshot):
        plan = snapshot['planning']['roadmap']['plan']
        receipts = [a for a in self.executions.acceptances() if a['sources'] == data['sources']]
        done = {a['slice_id'] for a in receipts}
        slices = [s for s in plan['slices'] if s['milestone'] == data['milestone'] and s['id'] not in done]
        if not slices:
            return None, 'milestone_ready'
        eligible = [s for s in slices if set(s['dependencies']) <= done]
        if not eligible:
            raise FactoryError('dependencies_unaccepted', 'Remaining milestone slices have unaccepted dependencies',
                               details={'slices': [s['id'] for s in slices]})
        ready = [s for s in eligible if s['maturity'] == 'execution_ready' and s['id'] in plan['near_term']]
        return (ready or eligible)[0], None

    def detail_horizon(self, data, plan, selected):
        done = {a['slice_id'] for a in self.executions.acceptances() if a['sources'] == data['sources']}
        detailed = {s['id'] for s in plan['slices'] if s['maturity'] == 'execution_ready'}
        with self.store._connection() as db:
            for row in db.execute('SELECT data FROM refinement_revisions'):
                revision = json.loads(row[0])
                if revision['sources'] == data['sources']:
                    detailed.add(revision['slice']['id'])
        if len((detailed - done) | {selected['id']}) > 3:
            raise FactoryError('detail_horizon', 'Three unaccepted slices are already detailed; resolve them before preparing another')

    def harness(self, data, slice_, plan, definition, worktree):
        from .execution_workspace import files
        inventory = files(worktree)
        locations = {h['id']: h['paths'] for h in definition['harness']}
        required = {h for g in gates_for(plan, slice_['id'], 'before_slice') + gates_for(plan, slice_['id'], 'after_slice') for h in g['harness']}
        receipts = {a['slice_id']: a for a in self.executions.acceptances() if a['sources'] == data['sources']}
        for h in plan['harness']:
            if h['id'] not in required or h['introduced_by'] == slice_['id']:
                continue
            receipt = receipts.get(h['introduced_by'])
            frozen = self.executions.definition(receipt['harness_revision']) if receipt and receipt.get('harness_revision') else None
            evidence = [e for e in receipt['evidence'] if e['gate'] in h['needed_by_gates']] if receipt else []
            if (not frozen or not evidence or any(e['status'] != 'PASS' for e in evidence) or
                not locations.get(h['id']) or any(inventory.get(p) != frozen['files'].get(p) for p in locations[h['id']])):
                raise FactoryError('harness_evidence_pending', 'A dependency receipt does not establish usable harness: ' + h['id'])

    def run(self, runtime_id):
        data = self.journal.latest()
        if not data or data['runtime_id'] != runtime_id or data['state'] in TERMINAL:
            return
        try:
            while True:
                data['integrated'] = reconcile(self.store)
                receipts = self.executions.acceptances()
                data['accepted'] = [{'slice_id': a['slice_id'], 'execution_id': a['execution_id'], 'commit': a['commit']}
                                    | {'kind': 'remediation' if a.get('remediation') else 'slice'}
                                    for a in receipts if a.get('continuation_id') == data['id']]
                self.journal.save(data)
                if self.runtime.paused():
                    self.stop(data, 'paused', 'Pause prevents new slice/refinement dispatches')
                    return
                self.runtime.update(runtime_id, 'running')
                snapshot, sources, definition, policy = Execution(self.service, self.store).prerequisites(check_selected=False)
                if sources != data['sources'] or policy != data['policy']:
                    raise FactoryError('stale_sources', 'Milestone run sources or authorization changed')
                with self.store._connection() as db:
                    check_budget(db, data['id'])
                closed = MilestoneStore(self.store).closed(sources)
                if data['milestone'] in closed:
                    if not self.advance(data, snapshot, closed):
                        return
                    continue
                # Recover a dispatch committed before its group pointer was saved.
                active = self.executions.latest()
                if active and active.get('continuation_id') == data['id'] and active['state'] != 'checkpoint':
                    data.update(execution_id=active['id'], active_slice=active['slice_id'], state=active['state'])
                    self.journal.save(data)
                    self.runtime.update(runtime_id, 'running')
                    Execution(self.service, self.store).run(runtime_id)
                    active = self.executions.get(active['id'])
                    if active['state'] != 'checkpoint':
                        if active.get('remediation') and active.get('architecture_proposal'):
                            journal = MilestoneStore(self.store)
                            for issue in journal.issues(data['milestone']):
                                if issue['id'] in active['remediation']['issues']:
                                    issue.update(classification='architecture_change', proposal=active['architecture_proposal'])
                                    journal.save_issue(data, issue)
                        self.stop(data, active['state'], active['reason'], active.get('diagnostic'))
                        return
                    data.update(execution_id=None, active_slice=None, remediation=None)
                    continue
                selected, stop = self.choose(data, snapshot)
                data.update(next_slice=selected['id'] if selected else None, active_slice=None, execution_id=None)
                if stop:
                    MilestoneGate(self, data, snapshot, definition).run()
                    continue
                if len(data['accepted']) >= data['limits']['max_slices']:
                    self.stop(data, 'checkpoint', 'Configured slice limit reached intentionally')
                    return
                with self.store._connection() as db:
                    check_budget(db, data['id'], dispatch=True)
                plan = snapshot['planning']['roadmap']['plan']
                errors = capability_errors(plan, selected, definition, policy, snapshot['architecture']['baseline']['architecture'])
                if errors:
                    raise FactoryError('capability_unavailable', 'Required slice capability is unsupported', details={'blockers': errors})
                if selected['architecture_impact'] == 'potential_change':
                    raise FactoryError('architecture_proposal', 'Slice requires structural review before implementation')
                refinement_id = None
                if selected['maturity'] != 'execution_ready' or selected['id'] not in plan['near_term']:
                    from .refinement import Refiner, effective_slice
                    self.detail_horizon(data, plan, selected)
                    data.update(state='preparing_milestone' if data.get('preparing_milestone') else 'refining', active_slice=selected['id'])
                    self.journal.save(data)
                    refinement_id = Refiner(self, data, selected, definition).run()
                    if not refinement_id:
                        return
                    selected = effective_slice(self.store, refinement_id)
                errors = check_coverage(plan, selected, definition)
                if errors:
                    raise FactoryError('verification_definition_missing', 'Bind mandatory checks before execution', details={'blockers': errors})
                # Ready slices need no model refinement. The existing execution engine
                # owns preflight, implementation, repair, verification and acceptance.
                execution = Execution(self.service, self.store).queue(runtime_id, data['id'] + ':' + selected['id'],
                    selected=selected, continuation=data, refinement_id=refinement_id)
                data.update(execution_id=execution['id'], refinement_id=None, active_slice=selected['id'], state='queued')
                data['preparing_milestone'] = False
                self.journal.save(data)
        except FactoryError as exc:
            if exc.code == 'stale_run':
                raise
            state = 'quota_blocked' if exc.code.startswith('quota_') else 'waiting_decision' if exc.code == 'waiting_decision' else (
                exc.code if exc.code in ('paused', 'budget_exhausted', 'validation_pending') else
                'validation_pending' if exc.code == 'stale_evidence' else 'blocked')
            self.stop(data, state, str(exc), {'code': exc.code, **exc.details})

    def advance(self, data, snapshot, closed):
        """Receipt precedes this recoverable transition; no inference for advancement."""
        plan = snapshot['planning']['roadmap']['plan']
        next_ = eligible_milestone(plan, closed)
        data.update(closed_milestones=[{'milestone': k, 'receipt': r['id'], 'commit': r['commit']} for k, r in closed.items()],
                    next_milestone=next_['id'] if next_ else None, active_gate=None, validation_id=None,
                    pending_gates=[], remediation=None, execution_id=None, active_slice=None)
        if len(closed) == len(plan['milestones']):
            self.stop(data, 'project_ready_for_validation', 'Roadmap milestones closed; final project validation remains pending')
            return False
        if not next_:
            raise FactoryError('milestone_dependencies_pending', 'No remaining milestone has satisfied dependencies')
        if not data['limits'].get('inter_milestone', False):
            self.stop(data, 'milestone_closed', 'Milestone closed; continuing between milestones requires explicit authorization')
            return False
        if len(data['accepted']) >= data['limits']['max_slices']:
            self.stop(data, 'checkpoint', 'Aggregate slice/remediation unit limit reached across milestones')
            return False
        with self.store._connection() as db:
            check_budget(db, data['id'])
        data.update(milestone=next_['id'], state='preparing_milestone', preparing_milestone=True,
                    next_milestone=None, reason=None, diagnostic=None)
        self.journal.save(data)
        return True
