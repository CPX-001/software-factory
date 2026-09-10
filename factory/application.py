"""Stable Factory use cases shared by CLI, MCP and detached workers."""
from dataclasses import asdict
from pathlib import Path
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
                 local_project=None, scope='default'):
        self.registry = registry or Registry()
        self.discovery_model, self.architecture_model = discovery_model, architecture_model
        self.planning_model = planning_model
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
                'capabilities': {'implemented_phases': ['discovery', 'architecture', 'planning'], 'planning_implemented': True}}

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
            return refinement_snapshot(snapshot, slice_id)
        if view == 'markdown':
            return roadmap['projection'] if roadmap else None
        if view == 'milestones':
            return {**metadata, 'milestones': plan['milestones'] if plan else []}
        if view == 'next_slice':
            candidates = executable_slices(plan) if roadmap else []
            return {**metadata, 'slice': candidates[0] if candidates else None,
                    'implementation_enabled': False}
        if view == 'requirements':
            slices = {s['id']: s for s in plan['slices']} if plan else {}
            coverage = {c['requirement']: c for c in plan['coverage']} if plan else {}
            items = []
            for r in snapshot['discovery']['knowledge']:
                if r['status'] == 'superseded':
                    continue
                c = coverage.get(r['key'])
                done = bool(roadmap and c and c['disposition'] == 'covered' and c['slices'] and
                            all(slices[s]['maturity'] == 'completed' for s in c['slices']))
                items.append({'key': r['key'], 'text': r['text'], 'coverage': c,
                              'pending': not done and (not c or c['disposition'] != 'out_of_scope')})
            return {**metadata, 'requirements': items}
        if view == 'verification':
            return {**metadata, 'gates': plan['gates'] if plan else [],
                    'harness': plan['harness'] if plan else []}
        raise FactoryError('invalid_view', 'Unknown planning view')

    def inspect(self, project=None, *, view, slice_id=None):
        if view in ('plan', 'milestones', 'next_slice', 'requirements', 'verification', 'refinement'):
            return self.get_planning(project, view=view, slice_id=slice_id)
        if slice_id is not None:
            raise FactoryError('invalid_arguments', 'slice_id is only valid for refinement')
        return self.get_architecture(project, view='baseline' if view == 'architecture' else view)

    def _planning(self, store):
        from .planning import Planning
        return Planning(store, self.planning_model, self.router, should_stop=Runtime(store).paused)

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
        return Discovery(store, self.discovery_model if self.discovery_model is not None else CodexDiscovery())

    def _architecture(self, store):
        return Architecture(store, self.architecture_model, self.router, should_stop=Runtime(store).paused)

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
        return self.get_status(project)

    def resume(self, project=None):
        project = self._project(project)['id']
        store = self._store(project); store.initialize()
        runtime = Runtime(store)
        with runtime.lock('launch'):
            runtime.unpause()
            if runtime.state()['status'] in ACTIVE or runtime.live():
                return self.get_status(project)
            from .controller import stop_reason
            reason = stop_reason(store.snapshot())
            if reason:
                return self.get_status(project)
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
