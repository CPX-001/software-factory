"""Stable Factory use cases shared by CLI, MCP and detached workers."""
from dataclasses import asdict
from pathlib import Path
import json
import subprocess
import sys

from .architecture import Architecture
from .discovery import Discovery
from .registry import FactoryError, Registry
from .runtime import ACTIVE, Runtime
from .workflow import Store, WorkflowError, next_action


def short(value, limit=400):
    text = str(value or '')
    return text if len(text) <= limit else text[:limit] + '…'


class FactoryService:
    def __init__(self, registry=None, *, discovery_model=None, architecture_model=None, planning_model=None, router=None, launcher=None,
                 local_project=None, scope='default', execution_worker_factory=None, refinement_worker_factory=None,
                 analysis_worker_factory=None):
        self.registry = registry or Registry()
        self.discovery_model, self.architecture_model = discovery_model, architecture_model
        self.planning_model = planning_model
        self.execution_worker_factory = execution_worker_factory
        self.refinement_worker_factory = refinement_worker_factory
        self.analysis_worker_factory = analysis_worker_factory
        self.router, self.launcher = router, launcher or self._launch
        self.local_project = Path(local_project).expanduser().resolve() if local_project is not None else None
        self.scope = scope

    @classmethod
    def for_local(cls, project, **kwargs):
        """Trusted CLI adapter binding. MCP never receives arbitrary local bindings."""
        return cls(local_project=project, **kwargs)

    def _project(self, project=None):
        if self.local_project is not None and project is None:
            return {'id': None, 'path': str(self.local_project), 'name': self.local_project.name}
        return self.registry.resolve(project, self.scope)

    def _store(self, project=None):
        target = self._project(project)
        Registry.validate_path(target['path'])
        return Store(target['path'])

    def initialize_project(self, path=None, name=None):
        if self.local_project is not None:
            self.registry.register(str(self.local_project), name, trusted=True)
            self._store().initialize()
        else:
            project = self.registry.register(path, name, create=True)
            self.registry.select(project['id'], self.scope)
        return self.get_status(project['id'] if self.local_project is None else None)

    def projects(self):
        items = self.registry.projects()
        return {'projects': items[:50], 'total': len(items)}

    def select_project(self, project):
        self.registry.select(project, self.scope)
        return self.get_status(project)

    def snapshot(self, project=None):
        """Full diagnostic representation for the CLI; status remains compact."""
        return self._store(project).snapshot()

    @staticmethod
    def _next(snapshot, runtime):
        if runtime['paused']:
            return {'action': 'resume', 'reason': 'paused'}
        if runtime['status'] in ACTIVE:
            return {'action': 'get_status', 'reason': 'run_in_progress'}
        if snapshot['phase'] in ('architecture', 'planning') and snapshot[snapshot['phase']]['stage'] == 'blocked':
            return {'action': 'resolve_blocker', 'reason': snapshot['phase'] + '_gate_or_budget'}
        return next_action(snapshot)

    def get_next_action(self, project=None):
        project = self._project(project)['id']
        if self.snapshot(project)['phase'] == 'execution':
            return self.get_status(project)['next_action']
        return self._next(self.snapshot(project), Runtime(self._store(project)).state())

    def list_pending_decisions(self, project=None, *, offset=0, limit=5):
        snapshot = self.snapshot(project)
        details = {d['id']: d for d in snapshot['discovery']['decision_details'] + snapshot['architecture']['decision_details'] + snapshot['planning']['decision_details']}
        pending = [d for d in snapshot['decisions'] if d['answer'] is None]
        items = []
        for d in pending[offset:offset + limit]:
            detail = details.get(d['id'], {})
            items.append({'id': d['id'], 'question': short(d['question'], 2000),
                          'reason': short(detail.get('why'), 500), 'options': detail.get('options', [])[:10],
                          'recommendation': short(detail.get('recommendation'), 2000),
                          'consequences': detail.get('consequences', [])[:10]})
        return {'items': items, 'total': len(pending), 'next_offset': offset + limit if offset + limit < len(pending) else None}

    def get_status(self, project=None):
        target = self._project(project)
        project = target['id']
        store = self._store(project)
        snapshot = store.snapshot()
        runtime = Runtime(store).state()
        discovery, architecture = snapshot['discovery'], snapshot['architecture']
        pending = [d for d in snapshot['decisions'] if d['answer'] is None]
        baseline = architecture['baseline']
        planning = snapshot['planning']
        blockers = (architecture['blockers'] + planning['blockers'])[:3]
        if runtime.get('error'):
            blockers = [runtime['error'], *blockers][:3]
        state = 'paused' if runtime['paused'] else 'working' if runtime['status'] in ACTIVE else snapshot['status']
        if runtime['status'] in ('failed', 'interrupted') and not runtime['paused']:
            state = runtime['status']
        action = self._next(snapshot, runtime)
        from .execution_store import ExecutionStore
        execution = ExecutionStore(store).inspect()
        continuation_policy = ExecutionStore(store).policy().get('continuation', {})
        execution['continuation_policy'] = continuation_policy
        if snapshot['phase'] == 'execution':
            if execution['state'] != 'not_started':
                state = execution['state']
                if runtime['paused'] and (runtime['status'] in ACTIVE or execution.get('sandbox_alive')):
                    state = 'pause_requested'
                elif runtime['paused']:
                    state = 'paused'
                blockers = list(dict.fromkeys((execution.get('blockers') or []) + blockers))[:5]
            action = {'action': 'authorize_execution' if not execution['enabled'] else
                      'get_status' if runtime['status'] in ACTIVE else
                      'wait_for_human' if pending else 'resume' if runtime['paused'] else
                      'execute_next_slice' if execution['state'] in ('not_started', 'checkpoint') else 'inspect_execution',
                      'reason': execution.get('reason')}
            if execution['state'] == 'not_started' and execution['enabled']:
                from .execution import Execution
                try:
                    Execution(self, store).prerequisites()
                except FactoryError as exc:
                    execution['diagnostic'] = {'code': exc.code, 'message': str(exc), **exc.details}
                    action.update(action='inspect_execution', reason=str(exc))
                if ExecutionStore(store).policy().get('analysis_authorized'):
                    execution['diagnostic'] = {'code': 'verification_binding_pending',
                        'message': 'Accepted planning still needs concrete verification bindings; analysis authorization is not an executable gate mapping.',
                        'gates': [g['id'] for g in snapshot['planning']['roadmap']['plan']['gates']],
                        'next_step': 'Bind the accepted plan without weakening the predeclared checks, delivery conditions or exclusion approvals; preserve consumed workflow budgets.'}
                    action.update(action='authorize_execution', reason=execution['diagnostic']['message'])
            if (execution.get('diagnostic') or {}).get('next_step'):
                action['next_step'] = execution['diagnostic']['next_step']
        from .continuation_store import ContinuationStore, TERMINAL
        continuation = ContinuationStore(store).inspect()
        if snapshot['phase'] == 'execution' and continuation and (
                continuation['state'] not in TERMINAL or
                runtime['run_id'] == continuation['runtime_id']):
            state = continuation['state']
            action = {'action': 'wait_for_human' if pending else 'resume' if runtime['paused'] else
                      'inspect_execution', 'reason': continuation['reason']}
            if continuation.get('diagnostic'):
                blockers = [continuation['diagnostic'].get('code', continuation['reason'])]
                if continuation['diagnostic'].get('next_step'):
                    action['next_step'] = continuation['diagnostic']['next_step']
        from .project_store import ProjectStore
        project_validation = ProjectStore(store).inspect()
        if project_validation['validated_version']:
            state = project_validation['state']
            action = {'action': 'inspect_project_validation', 'reason': state}
        elif state == 'project_ready_for_validation':
            action = {'action': 'validate_project', 'reason': 'Final project acceptance remains pending'}
        action = {k: v for k, v in action.items() if k not in ('readiness', 'pending_questions', 'blockers')}
        if 'decision_ids' in action:
            action['decision_ids'] = action['decision_ids'][:3]
        objective = next((i['text'] for i in discovery['knowledge'] if i['category'] == 'vision'), 'Clarify the project vision')
        return {'project': target, 'phase': snapshot['phase'], 'state': state, 'workflow_revision': snapshot['revision'],
                'current_objective': short(objective),
                'progress': {'knowledge_items': len(discovery['knowledge']), 'discovery_ready': bool(discovery['completed_at']),
                             'architecture_stage': architecture['stage'], 'architecture_calls': architecture['calls'],
                             'planning_stage': planning['stage'], 'planning_calls': planning['calls'],
                             'roadmap_revision': planning['roadmap']['revision'] if planning['roadmap'] else None},
                'last_message': short(discovery['last_message'], 1200) if snapshot['phase'] == 'discovery' else None,
                'questions': [{k: short(v, 500) for k, v in q.items()} for q in discovery['questions'][:3]],
                'pending_decisions': {'count': len(pending), 'items': [{'id': d['id'], 'question': short(d['question'])} for d in pending[:3]]},
                'blockers': [short(b, 500) for b in blockers], 'next_action': action,
                'architecture_revision': {k: baseline[k] for k in ('revision', 'fingerprint')} if baseline else None,
                'autonomous_run': {k: runtime.get(k) for k in ('run_id', 'status', 'paused', 'reason')},
                'execution': execution, 'continuation': continuation, 'project_validation': project_validation,
                'capabilities': {'implemented_phases': ['discovery', 'architecture', 'planning', 'execution'],
                                 'project_validation_implemented': True,
                                 'planning_implemented': True, 'execution_slice_limit': continuation_policy['max_slices'] if continuation_policy.get('enabled') else 1}}

    def get_discovery(self, project=None):
        return self.snapshot(project)['discovery']

    def get_architecture(self, project=None, *, view='baseline'):
        architecture = self.snapshot(project)['architecture']
        if view == 'baseline':
            baseline = architecture['baseline']
            return ({k: baseline[k] for k in ('revision', 'fingerprint', 'architecture', 'created_at')}
                    if baseline else {'baseline': None, 'stage': architecture['stage']})
        if view == 'revision':
            baseline = architecture['baseline']
            return {k: baseline[k] for k in ('revision', 'fingerprint')} if baseline else None
        if view == 'adrs':
            return architecture['adrs']
        if view == 'discovery':
            return self.get_discovery(project)
        if view == 'markdown':
            return architecture['baseline']['projection'] if architecture['baseline'] else None
        raise FactoryError('invalid_view', 'Unknown architecture view')

    def get_planning(self, project=None, *, view='plan', slice_id=None):
        from .planning import executable_slices, refinement_snapshot
        snapshot = self.snapshot(project)
        state = snapshot['planning']
        roadmap = state['roadmap']
        plan = roadmap['plan'] if roadmap else state['proposal']
        metadata = {'stage': state['stage'], 'accepted': roadmap is not None,
                    'revision': roadmap['revision'] if roadmap else None,
                    'architecture': plan['architecture'] if plan else None}
        if view == 'plan':
            return {**metadata, 'plan': plan, 'blockers': state['blockers']}
        if view == 'refinement':
            result = refinement_snapshot(snapshot, slice_id)
            with self._store(project)._connection() as db:
                available = db.execute('PRAGMA user_version').fetchone()[0] >= 7
                rows = db.execute('SELECT data FROM refinement_revisions').fetchall() if available else []
                units = db.execute('SELECT data FROM refinements').fetchall() if available else []
            revisions = [json.loads(row[0]) for row in rows]
            attempts = [json.loads(row[0]) for row in units]
            fields = ('id', 'slice_id', 'input_key', 'state', 'attempts', 'deadline', 'runtime_info',
                      'base_commit', 'revision_id', 'quota_before', 'quota', 'quota_after', 'quota_after_diagnostic',
                      'before_verification', 'errors', 'proposal', 'decision_ids')
            return {**result, 'execution_refinements': [r for r in revisions if r['slice']['id'] == slice_id],
                    'refinement_attempts': [{k: r.get(k) for k in fields} for r in attempts if r['slice_id'] == slice_id]}
        if view == 'markdown':
            return roadmap['projection'] if roadmap else None
        if view == 'milestones':
            from .milestone_store import MilestoneStore
            return {**metadata, **MilestoneStore(self._store(project)).progress(snapshot)}
        if view == 'next_slice':
            from .execution import select_slice
            from .execution_store import ExecutionStore
            journal = ExecutionStore(self._store(project))
            selected, reasons = select_slice(snapshot, journal.acceptances()) if roadmap else (None, [])
            from .continuation import Continuation
            controller = Continuation(self, self._store(project))
            group = controller.journal.latest()
            if group and journal.policy().get('continuation', {}).get('enabled'):
                try:
                    selected, reason = controller.choose(group, snapshot)
                    reasons = [reason] if reason else []
                except FactoryError as exc:
                    selected, reasons = None, [exc.code]
            return {**metadata, 'slice': selected, 'reasons': reasons,
                    'implementation_enabled': journal.policy()['enabled']}
        if view == 'requirements':
            from .milestone_store import MilestoneStore
            progress = {r['requirement']: r for r in MilestoneStore(self._store(project)).progress(snapshot)['requirements']}
            from .project_store import ProjectStore
            final = ProjectStore(self._store(project)).inspect(full=True)
            if final['validated_version'] and final['validated_version']['current']:
                receipt = final['receipts'][-1]
                for acceptance in receipt['requirement_acceptances']:
                    progress[acceptance['requirement']].update(status='satisfied', acceptance={**acceptance,
                        'receipt': receipt['id'], 'commit': receipt['commit'], 'scope': 'project'})
            slices = {s['id']: s for s in plan['slices']} if plan else {}
            coverage = {c['requirement']: c for c in plan['coverage']} if plan else {}
            items = []
            for r in snapshot['discovery']['knowledge']:
                if r['status'] == 'superseded':
                    continue
                c = coverage.get(r['key'])
                evidence = progress.get(r['key'], {})
                done = evidence.get('status') == 'satisfied'
                items.append({'key': r['key'], 'text': r['text'], 'coverage': c,
                              'progress': evidence,
                              'pending': not done and (not c or c['disposition'] != 'out_of_scope')})
            return {**metadata, 'requirements': items}
        if view == 'verification':
            from .milestone_store import MilestoneStore
            journal = MilestoneStore(self._store(project))
            from .project_store import ProjectStore
            return {**metadata, 'gates': plan['gates'] if plan else [],
                    'harness': plan['harness'] if plan else [],
                    'milestone_validations': journal.rows('milestone_validations'), 'closure_issues': journal.issues(),
                    'project_validation': ProjectStore(self._store(project)).inspect(full=True)}
        raise FactoryError('invalid_view', 'Unknown planning view')

    def inspect(self, project=None, *, view, slice_id=None):
        if view == 'project_validation':
            from .project_store import ProjectStore
            return ProjectStore(self._store(project)).inspect(full=True)
        if view == 'execution':
            from .execution_store import ExecutionStore
            from .continuation_store import ContinuationStore
            store = self._store(project)
            return {**ExecutionStore(store).inspect(full=True), 'continuation': ContinuationStore(store).inspect()}
        if view in ('plan', 'milestones', 'next_slice', 'requirements', 'verification', 'refinement'):
            return self.get_planning(project, view=view, slice_id=slice_id)
        if slice_id is not None:
            raise FactoryError('invalid_arguments', 'slice_id is only valid for refinement')
        return self.get_architecture(project, view='baseline' if view == 'architecture' else view)

    def _planning(self, store):
        from .planning import Planning
        from .execution_store import ExecutionStore
        return Planning(store, self._analysis_model(store, 'planning', self.planning_model), self.router,
                        should_stop=Runtime(store).paused,
                        allow_recovery=bool(ExecutionStore(store).policy().get('analysis_authorized')))

    def run_planning(self, project=None):
        store = self._store(project); store.initialize()
        runtime = Runtime(store)
        with runtime.lock('launch'), runtime.lock():
            if runtime.state()['status'] == 'queued':
                raise FactoryError('run_busy', 'A worker is already starting')
            return self._planning(store).run()

    def architecture_diagnostics(self, project=None):
        snapshot = self.snapshot(project)
        result = {k: v for k, v in snapshot['architecture'].items() if k not in ('source', 'proposal', 'baseline', 'adrs')}
        result['pending_decisions'] = [d for d in snapshot['decisions'] if d['answer'] is None]
        result['current'] = self.get_architecture(project, view='revision')
        return result

    def _discovery(self, store):
        from .codex_discovery import CodexDiscovery
        model = self._analysis_model(store, 'discovery', self.discovery_model)
        return Discovery(store, model if model is not None else CodexDiscovery())

    def _analysis_model(self, store, phase, fallback):
        from .execution_store import ExecutionStore
        if ExecutionStore(store).policy().get('analysis_authorized'):
            from .analysis_execution import AnalysisModel
            return AnalysisModel(self, store, phase)
        return fallback

    def _architecture(self, store):
        return Architecture(store, self._analysis_model(store, 'architecture', self.architecture_model), self.router, should_stop=Runtime(store).paused)

    def submit_user_message(self, message, project=None, *, request_id=None):
        project = self._project(project)['id']
        store = self._store(project)
        store.initialize()
        runtime = Runtime(store)
        with runtime.lock('launch'), runtime.lock():
            if runtime.state()['status'] == 'queued':
                raise FactoryError('run_busy', 'A worker is starting; inspect status before another input')
            snapshot = store.snapshot()
            if snapshot['phase'] == 'discovery':
                self._discovery(store).enqueue(message, request_id=request_id)
            else:
                pending = [d for d in snapshot['decisions'] if d['answer'] is None]
                if len(pending) == 1:
                    store.answer_decision(pending[0]['id'], message, snapshot['revision'])
                else:
                    raise FactoryError('message_needs_decision', 'Use a decision ID when several choices are pending; use resume to continue. New phase work is owned by Factory.')
        if not runtime.paused():
            self.resume(project)
        return self.get_status(project)

    def answer_decision(self, decision_id, answer, project=None, *, continue_run=True):
        project = self._project(project)['id']
        store = self._store(project)
        store.initialize()
        runtime = Runtime(store)
        with runtime.lock('launch'), runtime.lock():
            if runtime.state()['status'] == 'queued':
                raise FactoryError('run_busy', 'A worker is starting; inspect status before another input')
            snapshot = store.snapshot()
            existing = next((d for d in snapshot['decisions'] if d['id'] == decision_id), None)
            if existing and existing['answer'] == answer:
                pass  # Reconnected client retry: a confirmed answer is not applied twice.
            else:
                store.answer_decision(decision_id, answer, snapshot['revision'])
            snapshot = store.snapshot()
            if continue_run and snapshot['phase'] == 'discovery' and not any(d['answer'] is None for d in snapshot['decisions']):
                if snapshot['discovery']['pending_turn'] is None:
                    self._discovery(store).enqueue('Incorpora las decisiones humanas registradas y reevalúa discovery.')
        if continue_run and not runtime.paused():
            self.resume(project)
        return self.get_status(project)

    def pause(self, project=None):
        project = self._project(project)['id']
        store = self._store(project); store.initialize()
        Runtime(store).pause()
        from .execution_store import ExecutionStore
        execution = ExecutionStore(store).latest()
        if execution and execution['state'] != 'checkpoint':
            guard = store.path.parent / 'executions' / execution['id']
            guard.mkdir(parents=True, exist_ok=True)
            (guard / 'pause').touch()
        from .continuation import Continuation
        controller = Continuation(self, store)
        continuation = controller.journal.latest()
        if continuation:
            controller.pause(continuation)
        return self.get_status(project)

    def resume(self, project=None):
        project = self._project(project)['id']
        store = self._store(project); store.initialize()
        runtime = Runtime(store)
        if store.snapshot()['phase'] == 'execution':
            from .continuation import Continuation
            from .continuation_store import TERMINAL
            controller = Continuation(self, store)
            group = controller.journal.latest()
            if group and group.get('analysis'):
                return self.get_status(project)  # Accepted planning still needs the concrete check binding.
            if group and group['state'] == 'execution_authorized':
                controller.start('analysis-handoff-' + group['id'])
                return self.get_status(project)
            if group and (group['state'] not in TERMINAL or
                          runtime.state()['run_id'] == group['runtime_id']):
                controller.resume()
                return self.get_status(project)
            return self._resume_execution(project)
        with runtime.lock('launch'):
            runtime.unpause()
            if runtime.state()['status'] in ACTIVE or runtime.live():
                return self.get_status(project)
            from .controller import stop_reason
            if store.snapshot()['phase'] == 'planning':
                self._planning(store).queue_recovery()
            reason = stop_reason(store.snapshot())
            if reason:
                return self.get_status(project)
            from .continuation_store import ContinuationStore
            group = ContinuationStore(store).latest()
            if group and group.get('analysis'):
                run_id = group['runtime_id']
                runtime.update(run_id, 'queued', 'Resume the same workflow and persistent budget')
                group.update(state='analysis', reason=None, diagnostic=None)
                ContinuationStore(store).save(group)
            else:
                run_id = runtime.queue()
            target = self._project(project)
            if target['id'] is None:
                target = self.registry.register(target['path'], trusted=True)
            try:
                self.launcher(target['id'], run_id)
            except Exception as exc:
                runtime.update(run_id, 'failed', 'Worker could not start', str(exc)[:1000])
                raise FactoryError('worker_start_failed', 'Run intent is saved; resume to retry') from exc
        return self.get_status(project)

    def configure_execution(self, policy, verification, project=None):
        from .execution import configure
        store = self._store(project); store.initialize()
        runtime = Runtime(store)
        with runtime.lock('launch'), runtime.lock():
            if runtime.state()['status'] in ACTIVE:
                raise FactoryError('run_busy', 'A run is already starting')
            return configure(store, policy, verification)

    def execute_next_slice(self, project=None, *, request_id):
        """One durable intent per logical client request; selection belongs to Factory."""
        from .execution import Execution
        from .execution_store import ExecutionStore
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise FactoryError('invalid_request', 'Use a stable request_id for this execution request')
        project = self._project(project)['id']
        store = self._store(project); store.initialize()
        runtime, journal = Runtime(store), ExecutionStore(store)
        if journal.policy().get('continuation', {}).get('enabled'):
            from .continuation import Continuation
            identifier = Continuation(self, store).start(request_id)
            return {**self.get_status(project), 'requested_continuation_id': identifier}
        with runtime.lock('launch'):
            with store._connection() as db:
                prior = db.execute('SELECT execution_id FROM execution_requests WHERE id=?', (request_id,)).fetchone()
            if prior:
                return {**self.get_status(project), 'requested_execution_id': prior[0]}
            previous = journal.latest()
            if (previous and previous['state'] != 'checkpoint') or runtime.live() or runtime.state()['status'] in ACTIVE:
                if previous:
                    with store._connection(write=True) as db:
                        db.execute('INSERT INTO execution_requests VALUES (?,?)', (request_id, previous['id']))
                return self.get_status(project)
            engine = Execution(self, store)
            engine.prerequisites()
            run_id = runtime.queue()
            try:
                engine.queue(run_id, request_id)
            except Exception as exc:
                runtime.update(run_id, 'blocked', str(exc))
                raise
            self._launch_registered(project, run_id, runtime)
        return self.get_status(project)

    def validate_project(self, project=None, *, request_id, automatic_remediation=False):
        from .continuation import Continuation
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128 or type(automatic_remediation) is not bool:
            raise FactoryError('invalid_request', 'Use a stable request_id and explicit remediation authorization')
        project = self._project(project)['id']
        store = self._store(project); store.initialize()
        identifier = Continuation(self, store).start_validation(request_id, automatic_remediation=automatic_remediation)
        return {**self.get_status(project), 'requested_continuation_id': identifier}

    def _launch_registered(self, project, run_id, runtime):
        target = self._project(project)
        if target['id'] is None:
            target = self.registry.register(target['path'], trusted=True)
        try:
            self.launcher(target['id'], run_id)
        except Exception as exc:
            runtime.update(run_id, 'failed', 'Worker could not start', str(exc)[:1000])
            raise FactoryError('worker_start_failed', 'Intent is durable; resume to recover') from exc

    def _resume_execution(self, project):
        from .execution_store import ExecutionStore
        from .execution_sandbox import LinuxSandbox
        from .architecture import canonical
        store = self._store(project)
        runtime, journal = Runtime(store), ExecutionStore(store)
        with runtime.lock('launch'):
            runtime.unpause()
            data = journal.latest()
            if data:
                (store.path.parent / 'executions' / data['id'] / 'pause').unlink(missing_ok=True)
            if not data or data['state'] == 'checkpoint':
                if data:
                    from .integrated_code import reconcile
                    reconcile(store)
                if data and runtime.state()['run_id'] == data['run_id'] and not runtime.live():
                    runtime.update(data['run_id'], 'checkpoint', 'One-slice checkpoint recovered from its durable acceptance')
                return self.get_status(project)  # Updating/resuming never auto-enables a product run.
            if runtime.live() or runtime.state()['status'] in ACTIVE:
                return self.get_status(project)
            sandbox = LinuxSandbox(store.path.parent / 'executions' / data['id'])
            if sandbox.live():
                return self.get_status(project)  # A lost connection is not evidence of death.
            if any(d['answer'] is None for d in store.snapshot()['decisions']):
                return self.get_status(project)
            run_id = runtime.queue()
            data.update(run_id=run_id, state='queued')
            with store._connection(write=True) as db:
                db.execute('UPDATE executions SET run_id=?,state=?,data=? WHERE id=?',
                           (run_id, 'queued', canonical(data), data['id']))
                journal.event(db, data['id'], 'recovery_queued')
            self._launch_registered(project, run_id, runtime)
        return self.get_status(project)

    def _launch(self, project, run_id):
        # No client-supplied command, executable, module, environment or shell fragment.
        root = str(Path(__file__).resolve().parent.parent)
        process = subprocess.Popen([sys.executable, '-m', 'factory.runner', '--home', str(self.registry.home),
                                    '--project', project, '--run-id', run_id], cwd=root,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   start_new_session=True, close_fds=True)
        # Reap while this interface lives; detachment does not depend on this thread.
        import threading
        threading.Thread(target=process.wait, daemon=True).start()

    def run_pending(self, project, run_id):
        from .controller import Controller
        return Controller(self).run(project, run_id)

    def discovery_turn(self, message=None, project=None):
        """Synchronous compatibility use case; CLI chooses presentation, never a worker."""
        store = self._store(project); store.initialize()
        runtime = Runtime(store)
        with runtime.lock('launch'), runtime.lock():
            if runtime.state()['status'] == 'queued':
                raise FactoryError('run_busy', 'A worker is already starting')
            if runtime.paused():
                raise FactoryError('project_paused', 'Resume the project before processing input')
            engine = self._discovery(store)
            return engine.submit(message) if message is not None else engine.resume()

    def run_architecture(self, project=None):
        store = self._store(project); store.initialize()
        runtime = Runtime(store)
        with runtime.lock('launch'), runtime.lock():
            if runtime.state()['status'] == 'queued':
                raise FactoryError('run_busy', 'A worker is already starting')
            return self._architecture(store).run()

    def initialize_local(self):
        self._store().initialize()
        return self.snapshot()

    def authorize_root(self, path):
        """Local setup operation; deliberately not exposed by FactoryTools."""
        self.registry.allow_root(path)
        return {'allowed_root': str(Path(path).expanduser().resolve())}

    def skill_catalog(self, *, roots=(), config=None, work=None):
        from .skill_catalog import discover_catalog
        from .skill_router import SkillRouter, load_config
        project = self._project()['path']
        configured, policy = load_config(project, config)
        catalog = discover_catalog(project, (*configured, *roots))
        if work is None:
            return catalog.as_dict()
        routing = SkillRouter(catalog, policy).route(work)
        return {'work': asdict(work), 'routing': routing.as_dict(), 'catalog_errors': catalog.errors,
                'explicit_roots': catalog.explicit_roots}
