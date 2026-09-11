"""One-slice controller. Durable intent -> attempts -> independent checks -> checkpoint.

The limit is a run policy, not coordination in the plugin or a global conversation.
"""
import json
from pathlib import Path
import time
import uuid
import ast

from .architecture import canonical, fingerprint, SECTIONS
from .codex_execution import CodexExecution, quota_guard
from .quota import NEXT_STEPS, diagnose
from .execution_contract import (allowed, validate_policy, validate_result, validate_verification)
from .execution_sandbox import LinuxSandbox
from .execution_store import ExecutionStore
from .execution_workspace import (repository_identity, git, prepare, files, code_identity,
    apply_changes, tree_object, make_commit, publish_ref, repair_identity)
from .registry import FactoryError
from .runtime import Runtime
from .verification import Verifier, capability_errors, check_coverage, harness_errors, protected_harness
from .workflow import WorkflowError


def current_sources(snapshot):
    from .planning import source_snapshot
    architecture = snapshot['architecture']['baseline']
    roadmap = snapshot['planning']['roadmap']
    if snapshot['phase'] != 'execution' or not architecture or not roadmap:
        raise FactoryError('execution_not_ready', 'Execution needs accepted architecture and planning')
    binding = {k: architecture[k] for k in ('revision', 'fingerprint')}
    if (fingerprint(architecture['architecture']) != architecture['fingerprint'] or
            fingerprint(roadmap['plan']) != roadmap['fingerprint'] or not roadmap['gate']['passed'] or
            roadmap['plan']['architecture'] != binding or roadmap['source'] != source_snapshot(snapshot)):
        raise FactoryError('stale_sources', 'Architecture/planning/requirements revisions are no longer current')
    return {'architecture': binding, 'plan': {k: roadmap[k] for k in ('revision', 'fingerprint')}}


def select_slice(snapshot, accepted):
    """Plan array order is the deterministic tie-breaker; completion needs a receipt."""
    sources = current_sources(snapshot)
    plan = snapshot['planning']['roadmap']['plan']
    done = {a['slice_id'] for a in accepted if a['sources'] == sources}
    milestones = {m['id']: m for m in plan['milestones']}
    reasons = []
    for s in plan['slices']:
        if s['id'] in done:
            continue
        why = None
        if s['maturity'] != 'execution_ready' or s['id'] not in plan['near_term']:
            why = 'refinement_required'
        elif s['architecture'] != sources['architecture']:
            why = 'stale_slice_architecture'
        elif not set(s['dependencies']) <= done:
            why = 'dependencies_unaccepted'
        elif s['milestone'] not in milestones or milestones[s['milestone']]['status'] != 'open':
            why = 'milestone_unavailable'
        elif milestones[s['milestone']]['dependencies']:
            # No milestone closure receipts exist in this step. A model's "completed"
            # field cannot substitute for a future Factory closure receipt.
            why = 'milestone_dependencies_pending'
        if why:
            reasons.append({'slice': s['id'], 'reason': why})
        else:
            return s, reasons
    return None, reasons or [{'reason': 'all_prepared_slices_accepted'}]


def configure(store, policy, verification):
    validate_policy(policy)
    sources = current_sources(store.snapshot())
    plan = store.snapshot()['planning']['roadmap']['plan']
    validate_verification(verification, plan)
    journal = ExecutionStore(store)
    existing = journal.latest()
    from .continuation_store import ContinuationStore, TERMINAL
    continuation = ContinuationStore(store).latest()
    if continuation and continuation['state'] not in TERMINAL:
        raise FactoryError('run_busy', 'Continuation policy and verification remain frozen while its run is open')
    if existing and existing['state'] != 'checkpoint':
        raise FactoryError('execution_exists', 'Execution policy and checks are frozen while a run remains open')
    identity = repository_identity(store.project)
    definition = {'sources': sources, 'verification': verification}
    identifier = fingerprint(definition)
    authorized = {**policy, 'repository': identity, 'definition_id': identifier, 'authorized_at': time.time()}
    with store._connection(write=True) as db:
        db.execute('INSERT OR IGNORE INTO execution_definitions VALUES (?,?,?)',
                   (identifier, canonical(definition), time.time()))
        db.execute('UPDATE execution_policy SET data=? WHERE id=1', (canonical(authorized),))
        Runtime.event(db, 'execution_authorized' if policy['enabled'] else 'execution_disabled',
                      {'definition_id': identifier, 'repository': identity})
    limit = policy['continuation']['max_slices'] if policy.get('continuation', {}).get('enabled') else 1
    return {'policy': authorized, 'definition_id': identifier, 'slice_limit': limit}


class Execution:
    def __init__(self, service, store):
        self.service, self.store = service, store
        self.journal, self.runtime = ExecutionStore(store), Runtime(store)

    def prerequisites(self, *, data=None, check_selected=True):
        policy = self.journal.policy()
        if not policy['enabled']:
            raise FactoryError('execution_disabled', 'Explicitly authorize execution and its initial policy first')
        if self.runtime.paused():
            raise FactoryError('paused', 'Project is paused')
        snapshot = self.store.snapshot()
        if any(d['answer'] is None for d in snapshot['decisions']):
            raise FactoryError('waiting_decision', 'Pending human decisions block execution')
        if snapshot['architecture']['blockers'] or snapshot['planning']['blockers']:
            raise FactoryError('source_blocked', 'Architecture/planning has unresolved blockers')
        sources = current_sources(snapshot)
        if policy['repository'] != repository_identity(self.store.project):
            raise FactoryError('repository_mismatch', 'Repository differs from the explicitly authorized repository')
        definition = self.journal.definition(policy['definition_id'])
        if not definition or definition['sources'] != sources or (data and data['sources'] != sources):
            raise FactoryError('stale_sources', 'Execution or verification is bound to obsolete revisions')
        from .verification import execution_definition
        definition = {**definition, 'verification': execution_definition(snapshot['planning']['roadmap']['plan'], definition['verification'])}
        if data and (data['policy'] != policy or data['definition_id'] != policy['definition_id']):
            raise FactoryError('policy_changed', 'Execution authorization changed')
        selected = data['slice'] if data else select_slice(snapshot, self.journal.acceptances())[0] if check_selected else None
        if data and data.get('refinement_id'):
            from .refinement import effective_slice
            if effective_slice(self.store, data['refinement_id']) != selected:
                raise FactoryError('stale_refinement', 'Execution refinement differs from its immutable revision')
        if data:
            # The approved roadmap remains immutable; the execution has a separately
            # versioned refinement overlay. Source checks above use the original plan.
            snapshot['planning']['roadmap']['plan']['slices'] = [selected if s['id'] == selected['id'] else s
                for s in snapshot['planning']['roadmap']['plan']['slices']]
            if data.get('remediation'):
                from .milestone import remediation_view
                plan, checks = remediation_view(snapshot['planning']['roadmap']['plan'], definition['verification'], data)
                snapshot['planning']['roadmap']['plan'] = plan
                definition = {**definition, 'verification': checks}
        if selected:
            errors = capability_errors(snapshot['planning']['roadmap']['plan'], selected,
                                       definition['verification'], policy, snapshot['architecture']['baseline']['architecture'])
            if errors:
                raise FactoryError('capability_unavailable', 'This slice requires capabilities outside the current Python/unittest mode',
                    details={'blockers': errors, 'next_step': 'Use a slice supported by Python/unittest or wait for the required executor capability. Do not weaken mandatory checks.'})
        return snapshot, sources, definition['verification'], policy

    def queue(self, run_id, request_id, *, selected=None, continuation=None, refinement_id=None, remediation=None, failure=None):
        with self.store._connection() as db:
            prior = db.execute('SELECT execution_id FROM execution_requests WHERE id=?', (request_id,)).fetchone()
        if prior:
            return self.journal.get(prior[0])
        snapshot, sources, definition, policy = self.prerequisites(check_selected=continuation is None)
        reasons = []
        if selected is None:
            selected, reasons = select_slice(snapshot, self.journal.acceptances())
        if not selected:
            raise FactoryError('no_eligible_slice', 'No prepared slice has satisfied dependencies', details={'reasons': reasons})
        plan = snapshot['planning']['roadmap']['plan']
        if remediation:
            from .milestone import remediation_view
            plan, definition = remediation_view(plan, definition, {'slice': selected, 'remediation': remediation})
        errors = check_coverage(plan, selected, definition)
        if errors:
            raise FactoryError('verification_definition_missing', 'Bind all required checks and acceptance criteria first', details={'blockers': errors})
        identifier = str(uuid.uuid4())
        from .integrated_code import reconcile
        base = reconcile(self.store)['commit']
        now = time.time()
        data = {'id': identifier, 'run_id': run_id, 'slice_id': selected['id'], 'slice': selected,
                'sources': sources, 'definition_id': policy['definition_id'], 'policy': policy,
                'repository': policy['repository'], 'base_commit': base, 'branch': 'factory/slice-' + identifier,
                'worktree': str(self.store.path.parent / 'worktrees' / identifier),
                'state': 'queued', 'created_at': now, 'updated_at': now, 'deadline': now + policy['max_seconds'],
                'attempt': 0, 'attempt_token': None, 'usage': None, 'quota': None, 'verification': [],
                'blockers': [], 'last_event': 'queued', 'reason': None, 'thread_id': None, 'commit': None}
        if continuation:
            data.update(continuation_id=continuation['id'], refinement_id=refinement_id,
                        deadline=min(data['deadline'], continuation['deadline']))
        if remediation:
            data.update(remediation=remediation, failure=failure)
        with self.store._connection(write=True) as db:
            db.execute('INSERT INTO executions VALUES (?,?,?,?,?,?)', (identifier, run_id, 'queued', canonical(data), now, now))
            db.execute('INSERT INTO execution_requests VALUES (?,?)', (request_id, identifier))
            self.journal.event(db, identifier, 'queued')
        return data

    def stop(self, data, state, reason, *, blockers=None):
        data.update(state=state, reason=reason, blockers=blockers or [])
        self.journal.save(data)
        self.runtime.update(data['run_id'], state, reason)

    def remaining(self, data):
        return data['deadline'] - time.time()

    def interruption(self, data):
        if self.runtime.paused():
            return 'paused'
        if self.remaining(data) <= 0:
            return 'budget_exhausted'
        usage = data.get('usage')
        if usage and usage.get('total', {}).get('totalTokens', 0) >= data['policy']['max_tokens']:
            return 'budget_exhausted'
        if data.get('continuation_id'):
            from .continuation_store import check_budget
            try:
                with self.store._connection() as db:
                    check_budget(db, data['continuation_id'])
            except FactoryError:
                return 'budget_exhausted'
        return False

    def routing(self, slice_, *, intent='implement'):
        from .skill_catalog import discover_catalog
        from .skill_router import SkillRouter, Work, load_config
        from .planning import classify
        snapshot = self.store.snapshot()
        characteristics = classify(snapshot['planning']['roadmap']['source'])
        router = self.service.router
        if router is None:
            roots, policy = load_config(self.store.project)
            router = SkillRouter(discover_catalog(self.store.project, roots), policy)
        routing = router.route(Work(domain=characteristics.get('domain', 'general'), intent=intent,
            risk=characteristics.get('risk', 'normal'), ui=characteristics.get('ui', False),
            concerns=tuple(slice_['verification_triggers'])))
        try:
            return routing, routing.required_inputs(router.catalog)
        except WorkflowError as exc:
            raise FactoryError('skill_unavailable', str(exc)) from exc

    def capsule(self, data, definition, routing, *, repair=False):
        if data.get('remediation'):
            data = {**data, 'policy': {**data['policy'], 'write_paths': data['remediation']['write_paths']}}
        snapshot = self.store.snapshot()
        baseline = snapshot['architecture']['baseline']['architecture']
        slice_ = data['slice']
        inventory = files(data['worktree'])
        roots = list(dict.fromkeys(data['policy']['context_paths'] + data['policy']['write_paths']))
        paths = [p for p in inventory if allowed(p, roots)]
        extra = data.get('read_paths', [])
        selected = list(dict.fromkeys(extra + paths))
        content = {}
        unavailable = {}
        size = 0
        for path in selected:
            if not allowed(path, roots) or path not in inventory:
                if path in extra:
                    unavailable[path] = 'outside_authorized_context' if not allowed(path, roots) else 'not_found'
                continue
            raw = (Path(data['worktree']) / path).read_bytes()
            if len(raw) > 12000 or size + len(raw) > 40000:
                if path in extra:
                    unavailable[path] = 'context_size_limit'
                continue
            try:
                content[path] = raw.decode()
            except UnicodeDecodeError:
                if path in extra:
                    unavailable[path] = 'not_utf8'
                continue
            size += len(raw)
        references = set(slice_['components'] + slice_['boundaries'])
        def relevant(item):
            linked = set(item.get('components', []) + item.get('participants', []) + item.get('steps', []))
            linked.update(item.get(k) for k in ('source', 'target', 'owner', 'component') if item.get(k))
            return item['id'] in references or bool(linked & references)
        architecture = {section: [item for item in baseline[section] if relevant(item)]
                        for section in SECTIONS}
        architecture['components'] = [c for c in baseline['components'] if c['id'] in references]
        context = {'slice': slice_, 'source_revisions': data['sources'], 'architecture': architecture,
            'requirements': [k for k in snapshot['discovery']['knowledge'] if k['key'] in slice_['requirements']],
            'satisfied_dependencies': slice_['dependencies'], 'files': content, 'file_inventory': paths[:300],
            'omitted_files': len(paths) - len(content), 'permissions': {'write_paths': data['policy']['write_paths'],
            'network': False, 'commands': False, 'publication': 'Factory only'},
            'skills': routing.context(), 'verification': definition,
            'answers': [d for d in snapshot['decisions'] if d['id'] in data.get('decision_ids', [])],
            'attempt': data['attempt'], 'failure': data.get('failure')}
        context['context_requests'] = {'included': [p for p in extra if p in content], 'unavailable': unavailable}
        if data.get('refinement_id') and not repair:
            from .refinement import revision
            context['refinement'] = revision(self.store, data['refinement_id'])['proposal']
        if repair and data.get('thread_id'):
            context = {k: context[k] for k in ('files', 'attempt', 'failure', 'answers', 'permissions', 'context_requests')}
        if len(canonical(context).encode()) > 100000:
            raise FactoryError('context_too_large', 'Slice capsule exceeds 100 KB; narrow the prepared scope')
        return context

    def architecture_impacts(self, data, result):
        baseline, current = data['initial_files'], files(data['worktree'])
        changed = [p for p in set(baseline) | set(current) if baseline.get(p) != current.get(p)]
        manifests = {'pyproject.toml', 'requirements.txt', 'package.json', 'package-lock.json',
                     'Cargo.toml', 'Cargo.lock', 'go.mod', 'go.sum', 'Gemfile', 'poetry.lock'}
        signals = [p for p in changed if Path(p).name in manifests or p.endswith('.sql') or
                   set(Path(p).parts) & {'migrations', 'schema', 'schemas'}]
        for path in changed:
            if not path.endswith('.py') or path not in current:
                continue
            try:
                tree = ast.parse((Path(data['worktree']) / path).read_text())
                imports = {node.module.split('.')[0] for node in ast.walk(tree)
                           if isinstance(node, ast.ImportFrom) and node.module}
                imports.update(a.name.split('.')[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for a in node.names)
                if imports & {'sqlite3', 'sqlalchemy', 'psycopg', 'psycopg2', 'redis', 'pymongo', 'boto3'} and path not in baseline:
                    signals.append(path)
            except (SyntaxError, UnicodeError):
                pass  # The real verifier will report syntax failures; no semantic claim here.
        if signals or result['architecture']['impact'] == 'proposal' or data['slice']['architecture_impact'] == 'potential_change':
            return {'reason': result['architecture']['reason'] or data['slice']['impact_reason'] or 'Structural file changes require review',
                    'paths': signals, 'references': result['architecture']['references'], 'sources': data['sources'],
                    'limit': 'Mechanical rules do not identify all semantic architecture changes'}
        return None

    def validate_scope(self, data):
        current = files(data['worktree'])
        changed = {p for p in set(data['initial_files']) | set(current) if data['initial_files'].get(p) != current.get(p)}
        roots = data['remediation']['write_paths'] if data.get('remediation') else data['policy']['write_paths']
        if any(not allowed(p, roots) for p in changed):
            raise FactoryError('scope_violation', 'Actual worktree changes exceed authorized product paths')

    def decide(self, data, questions, *, structural=False):
        with self.store._connection(write=True) as db:
            ids = []
            for question in questions:
                ids.append(db.execute('INSERT INTO decisions(question) VALUES (?)', (question,)).lastrowid)
            revision = db.execute('SELECT revision FROM workflow WHERE id=1').fetchone()[0]
            self.store._record(db, revision + 1, 'execution_decisions_requested', {'execution': data['id'], 'ids': ids})
        data['decision_ids'] = list(dict.fromkeys(data.get('decision_ids', []) + ids))
        self.stop(data, 'blocked' if structural else 'waiting_decision',
                  'Architectural proposal needs a revised baseline; automatic architect is disabled' if structural else 'Waiting for human answers')

    def run(self, run_id):
        data = self.journal.latest()
        if not data or data['run_id'] != run_id or data['state'] == 'checkpoint':
            return
        sandbox = LinuxSandbox(self.store.path.parent / 'executions' / data['id'])
        sandbox.lifetime = max(1, self.remaining(data))
        worker = None
        attempt = None
        try:
            if sandbox.live():
                self.stop(data, 'interrupted', 'An earlier sandbox is still alive; recovery will not launch a duplicate')
                return
            snapshot, _, definition, _ = self.prerequisites(data=data)
            plan = snapshot['planning']['roadmap']['plan']
            if data.get('architecture_proposal'):
                self.stop(data, 'blocked', 'Architectural proposal retained; baseline revision required')
                return
            reason = self.interruption(data)
            if reason:
                self.stop(data, reason, 'Persistent run limit or pause reached')
                return
            data.update(state='preflight', blockers=[], reason=None, diagnostic=None)
            self.journal.save(data)
            data['isolation'] = sandbox.probe()
            prepare(self.store, data)
            if 'initial_files' not in data:
                if data.get('refinement_id'):
                    from .refinement import revision
                    inputs = revision(self.store, data['refinement_id'])['input']['files']
                    if any(not (Path(data['worktree']) / p).is_file() or
                           (Path(data['worktree']) / p).read_text() != content for p, content in inputs.items()):
                        raise FactoryError('stale_refinement', 'Relevant code differs from the accepted refinement input')
                data['initial_files'] = files(data['worktree'])
                data['protected_files'] = protected_harness(definition, data['initial_files'])
                self.journal.save(data, kind='workspace_prepared')
            errors = harness_errors(plan, data['slice'], definition, data['worktree'])
            if errors:
                self.stop(data, 'blocked', 'Pre-existing harness is required', blockers=errors)
                return
            if data.get('continuation_id'):
                from .continuation import Continuation
                Continuation(self.service, self.store).harness(data, data['slice'], plan, definition, data['worktree'])
            routing, skills = self.routing(data['slice'])
            verifier = Verifier(sandbox)
            def process(ref):
                data['process'] = ref
                self.journal.save(data, kind='verification_started')
            # Existing worker response is a recovery checkpoint. Never repeat the model
            # call merely because its process or MCP connection disappeared.
            attempts = self.journal.attempts(data['id'])
            recovered = attempts[-1] if attempts and attempts[-1]['state'] in ('responded', 'applied', 'verified') else None
            if attempts and attempts[-1]['state'] in ('started', 'interrupted') and attempts[-1].get('runtime'):
                previous = attempts[-1]
                data['thread_id'] = previous['runtime']['thread_id']
                sandbox.lifetime = max(.1, self.remaining(data))
                worker = (self.service.execution_worker_factory or CodexExecution)(sandbox, data['policy'], data['worktree'], skills)
                try:
                    outcome = worker.recover(previous['runtime'])
                    if outcome:
                        validate_result(outcome['response'])
                        previous.update(response=outcome['response'], worker_status=outcome['worker_status'],
                                        state='responded', recovered_at=time.time())
                        self.journal.save_attempt(data, previous)
                        self.journal.save(data, kind='runtime_result_recovered')
                        recovered = previous
                    else:
                        previous.update(state='interrupted_confirmed', recovered_at=time.time())
                        self.journal.save_attempt(data, previous)
                finally:
                    worker.close()
                    worker = None
            if data.get('publication'):
                self.accept(data)
                return
            if not data.get('before_verified'):
                before = verifier.run(plan, data['slice'], definition, data['worktree'], trigger='before_slice',
                    should_stop=lambda: self.interruption(data), on_process=process, remaining=lambda: self.remaining(data))
                data['before_verification'] = before
                if any(e['status'] != 'PASS' for e in before):
                    data['verification'] = before
                    self.stop(data, 'validation_pending', 'Pre-slice gate did not pass')
                    return
                data['before_verified'] = True
                self.journal.save(data, kind='pre_slice_verified')
            while True:
                reason = self.interruption(data)
                if reason:
                    self.stop(data, reason, 'Persistent run limit or pause reached')
                    return
                self.prerequisites(data=data)
                if recovered:
                    attempt = recovered
                    result = attempt['response']
                    recovered = None
                else:
                    if len(self.journal.attempts(data['id'])) >= data['policy']['max_attempts']:
                        self.stop(data, 'budget_exhausted', 'One implementation and at most two repairs exhausted')
                        return
                    # Fresh credentials/model availability/quota are checked before EVERY attempt.
                    sandbox.lifetime = max(.1, self.remaining(data))
                    worker = (self.service.execution_worker_factory or CodexExecution)(sandbox, data['policy'], data['worktree'], skills)
                    data['runtime_info'] = getattr(worker, 'runtime_info', None)
                    try:
                        data['quota'] = worker.quota()
                    except Exception as exc:
                        data['quota'] = None
                        raise diagnose(exc) from exc
                    data.setdefault('quota_before', data['quota'])
                    quota_guard(data['quota'], data['policy'])
                    self.journal.save(data, kind='quota_checked')
                    attempt = self.journal.start_attempt(data)
                    def on_runtime(ref):
                        attempt['runtime'] = ref
                        attempt['runtime_info'] = data.get('runtime_info')
                        data['thread_id'] = ref['thread_id']
                        data['process'] = ref.get('process')
                        self.journal.save_attempt(data, attempt)
                        self.journal.save(data, token=attempt['token'], kind='worker_started')
                    def on_usage(usage):
                        data['usage'] = usage
                        attempt['usage'] = usage
                        self.journal.save_attempt(data, attempt)
                        self.journal.save(data, token=attempt['token'], kind='usage_observed')
                    def on_quota(quota):
                        data['quota'] = quota
                        self.journal.save(data, token=attempt['token'], kind='quota_observed')
                    try:
                        outcome = worker.respond(self.capsule(data, definition, routing, repair=data['attempt'] > 1),
                            thread_id=data.get('thread_id'), should_stop=lambda: self.interruption(data),
                            on_runtime=on_runtime, on_usage=on_usage, on_quota=on_quota)
                        result = outcome['response']
                        if 'quota_after' in outcome:
                            data['quota'] = data['quota_after'] = outcome['quota_after']
                        if 'quota_after_diagnostic' in outcome:
                            data['quota_after_diagnostic'] = outcome['quota_after_diagnostic']
                        validate_result(result)
                        attempt.update(response=result, worker_status=outcome['worker_status'], state='responded', completed_at=time.time())
                        self.journal.save_attempt(data, attempt)
                    except (WorkflowError, ValueError, TypeError, KeyError) as exc:
                        if isinstance(exc, FactoryError) and exc.code != 'invalid_output':
                            raise
                        attempt.update(state='invalid_output', worker_status='invalid_output', error=str(exc)[:1500], completed_at=time.time())
                        self.journal.save_attempt(data, attempt)
                        data['failure'] = {'kind': 'invalid_output', 'error': str(exc)[:1500]}
                        signature = fingerprint({'code': repair_identity(data['worktree']), 'failure': data['failure']})
                        if data.get('failure_signature') == signature:
                            self.stop(data, 'failed', 'Same invalid output repeated without relevant changes')
                            return
                        data['failure_signature'] = signature
                        self.journal.save(data, kind='invalid_output')
                        continue
                    finally:
                        worker.close()
                        worker = None
                validate_result(result)
                self.prerequisites(data=data)
                if self.interruption(data):
                    self.stop(data, str(self.interruption(data)), 'Result saved; pause/limit prevents applying changes')
                    return
                # Staged, idempotent file application only after the response is durable.
                if attempt['state'] == 'responded':
                    data['state'] = 'applying'
                    self.journal.save(data, token=attempt['token'])
                    try:
                        policy = {**data['policy'], 'write_paths': data['remediation']['write_paths']} if data.get('remediation') else data['policy']
                        apply_changes(data['worktree'], result, policy, protected_files=data['protected_files'])
                    except FactoryError as exc:
                        if exc.code != 'application_conflict':
                            raise
                        attempt.update(state='application_failed', error=str(exc), completed_at=time.time())
                        self.journal.save_attempt(data, attempt)
                        data['failure'] = {'kind': exc.code, 'message': str(exc), **exc.details}
                        signature = fingerprint({'code': repair_identity(data['worktree']), 'failure': data['failure']})
                        if data.get('failure_signature') == signature:
                            self.stop(data, 'failed', 'Same application conflict repeated without relevant changes')
                            return
                        data['failure_signature'] = signature
                        self.journal.save(data, kind='repair_needed')
                        continue
                    attempt['state'] = 'applied'
                    self.journal.save_attempt(data, attempt)
                proposal = self.architecture_impacts(data, result)
                self.validate_scope(data)
                if proposal:
                    data['architecture_proposal'] = proposal
                    self.journal.save(data, kind='architecture_proposal')
                    self.decide(data, [proposal['reason']], structural=True)
                    return
                if result['questions'] and not attempt.get('questions_recorded'):
                    attempt['questions_recorded'] = True
                    attempt['state'] = 'question'
                    self.journal.save_attempt(data, attempt)
                    self.decide(data, result['questions'])
                    return
                if result['read_paths']:
                    data['read_paths'] = result['read_paths']
                    data['failure'] = {'kind': 'context_requested', 'paths': result['read_paths']}
                    attempt['state'] = 'context_requested'
                    self.journal.save_attempt(data, attempt)
                    self.journal.save(data, kind='context_requested')
                    continue
                missing = harness_errors(plan, data['slice'], definition, data['worktree'], after=True)
                # Freeze introduced tests before the first result. Repairs may fix product
                # code but cannot make a failing required test disappear or pass trivially.
                current_harness = protected_harness(definition, files(data['worktree']))
                if any(current_harness.get(p) != value for p, value in data['protected_files'].items()):
                    raise FactoryError('verification_weakened', 'Frozen harness changed outside the current attempt')
                data['protected_files'].update(current_harness)
                materialized = {'kind': 'materialized_harness', 'authorized_definition_id': data['definition_id'],
                                'sources': data['sources'], 'files': data['protected_files']}
                data['harness_revision'] = fingerprint(materialized)
                with self.store._connection(write=True) as db:
                    db.execute('INSERT OR IGNORE INTO execution_definitions VALUES (?,?,?)',
                        (data['harness_revision'], canonical(materialized), time.time()))
                data['state'] = 'verifying'
                self.journal.save(data, token=attempt['token'])
                evidence = verifier.run(plan, data['slice'], definition, data['worktree'], trigger='after_slice',
                    should_stop=lambda: self.interruption(data), on_process=process, remaining=lambda: self.remaining(data))
                data['verification'] = evidence
                attempt['verification'] = evidence
                attempt['verification_status'] = ('NOT_RUN' if missing or any(e['status'] == 'NOT_RUN' for e in evidence)
                    else 'FAIL' if any(e['status'] == 'FAIL' for e in evidence) else 'PASS')
                attempt['state'] = 'verified'
                self.journal.save_attempt(data, attempt)
                self.journal.save(data, kind='verification_finished')
                reason = self.interruption(data)
                if reason:
                    self.stop(data, reason, 'Verification interrupted; changes retained')
                    return
                if missing:
                    failure = {'kind': 'harness_missing', 'blockers': missing}
                elif attempt['verification_status'] == 'NOT_RUN':
                    self.stop(data, 'validation_pending', 'Mandatory validation is unavailable', blockers=[e.get('reason') for e in evidence if e['status'] == 'NOT_RUN'])
                    return
                elif attempt['verification_status'] == 'PASS':
                    if set(result['criteria_addressed']) != set(range(len(data['slice']['acceptance_criteria']))):
                        failure = {'kind': 'criteria_unaddressed'}
                    else:
                        self.accept(data)
                        return
                else:
                    failure = {'kind': 'code_failure', 'checks': [e for e in evidence if e['status'] != 'PASS']}
                signature = fingerprint({'code': repair_identity(data['worktree']), 'kind': failure['kind'],
                    'failures': [(e['check_id'], e['status'], e.get('exit_code')) for e in evidence if e['status'] != 'PASS']})
                if data.get('failure_signature') == signature:
                    self.stop(data, 'failed', 'Same failure without relevant code changes; repair stopped early')
                    return
                data.update(failure=failure, failure_signature=signature)
                attempt['state'] = 'failed_verification'
                self.journal.save_attempt(data, attempt)
                self.journal.save(data, kind='repair_needed')
        except FactoryError as exc:
            if self.journal.get(data['id'])['state'] == 'checkpoint':
                raise  # Acceptance is durable; recovery must reconcile publication, never undo it.
            if exc.code in NEXT_STEPS:
                exc = diagnose(exc)
            mapping = {'paused': 'paused', 'budget_exhausted': 'budget_exhausted', 'waiting_decision': 'waiting_decision',
                       'quota_unknown': 'quota_blocked', 'quota_reserve': 'quota_blocked', 'quota_exhausted': 'quota_blocked',
                       'authentication': 'blocked', 'model_unavailable': 'blocked', 'isolation_unavailable': 'blocked',
                       'infrastructure_failed': 'infrastructure_failed', 'worker_failed': 'infrastructure_failed',
                       'stale_evidence': 'validation_pending'}
            if attempt and attempt['state'] == 'started':
                attempt.update(state='interrupted', error=exc.code, completed_at=time.time())
                self.journal.save_attempt(data, attempt)
            if worker:
                worker.close()
                worker = None
            if exc.code.startswith('stale_attempt') or exc.code == 'stale_execution':
                raise
            data['diagnostic'] = {'code': exc.code, 'message': str(exc), **exc.details}
            self.stop(data, 'quota_blocked' if exc.code.startswith('quota_') else mapping.get(exc.code, 'blocked'),
                      str(exc), blockers=[exc.code])
        except Exception as exc:
            if self.journal.get(data['id'])['state'] == 'checkpoint':
                raise
            if worker:
                worker.close()
                worker = None
            reason = self.interruption(data)
            self.stop(data, str(reason) if reason else 'infrastructure_failed',
                      'Active process stopped for pause/budget' if reason else f'{type(exc).__name__}: {str(exc)[:1000]}')
        finally:
            if worker:
                worker.close()

    def accept(self, data):
        snapshot, _, definition, _ = self.prerequisites(data=data)
        self.validate_scope(data)
        evidence = data['verification']
        expected = {c['id'] for c in definition['checks'] if c['gate'] in
            {g['id'] for g in snapshot['planning']['roadmap']['plan']['gates']
             if g['target'] == data['slice_id'] and g['trigger'] == 'after_slice'}}
        identity = code_identity(data['worktree'])
        if (not evidence or {e['check_id'] for e in evidence} != expected or
                any(e['status'] != 'PASS' or e['code_id'] != identity for e in evidence)):
            raise FactoryError('stale_evidence', 'Acceptance needs all required PASS evidence for this exact code')
        if self.interruption(data):
            raise FactoryError(str(self.interruption(data)), 'Pause or budget prevents acceptance')
        if not data.get('publication'):
            directory = self.store.path.parent / 'executions' / data['id']
            tree = tree_object(data['worktree'], directory / 'publication.index')
            if code_identity(data['worktree']) != identity:
                raise FactoryError('stale_evidence', 'Code changed while preparing commit')
            commit = make_commit(data['worktree'], tree, data['base_commit'], data['id'], data['created_at'])
            data.update(state='publishing', publication={'tree': tree, 'commit': commit, 'code_id': identity})
            self.journal.save(data, kind='publication_intent')
        publication = data['publication']
        if publication['code_id'] != identity:
            raise FactoryError('stale_evidence', 'Publication intent targets another version of code')
        publish_ref(data['worktree'], data['branch'], publication['commit'], data['base_commit'])
        # The immutable commit and its exact tree exist before the SQLite receipt. Replay
        # reuses the same intent/commit and compare-and-swap ref update.
        receipt = {'execution_id': data['id'], 'slice_id': data['slice_id'], 'sources': data['sources'],
                   'definition_id': data['definition_id'], 'code_id': identity, 'commit': publication['commit'],
                   'tree': publication['tree'], 'branch': data['branch'], 'evidence': evidence,
                   'before_evidence': data.get('before_verification', []), 'accepted_at': time.time(),
                   'harness_revision': data.get('harness_revision'),
                   'scope': 'slice_only', 'unverified': 'Milestone closure and global product requirements'}
        receipt.update(continuation_id=data.get('continuation_id'), refinement_id=data.get('refinement_id'),
                       base_commit=data['base_commit'], remediation=data.get('remediation'))
        current_files = files(data['worktree'])
        receipt['changed_paths'] = sorted(p for p in set(data['initial_files']) | set(current_files)
                                          if data['initial_files'].get(p) != current_files.get(p))
        self.prerequisites(data=data)
        if code_identity(data['worktree']) != identity:
            raise FactoryError('stale_evidence', 'Worktree changed before the acceptance receipt')
        with self.store._connection(write=True) as db:
            control = db.execute('SELECT * FROM factory_control WHERE id=1').fetchone()
            if control['run_id'] != data['run_id'] or control['paused']:
                raise FactoryError('stale_execution', 'Acceptance ownership changed or paused')
            if db.execute('SELECT 1 FROM decisions WHERE answer IS NULL').fetchone():
                raise FactoryError('waiting_decision', 'Decision opened before acceptance')
            current = json.loads(db.execute('SELECT data FROM executions WHERE id=?', (data['id'],)).fetchone()[0])
            if current.get('attempt_token') != data.get('attempt_token'):
                raise FactoryError('stale_attempt', 'Attempt changed before acceptance')
            db.execute('INSERT INTO execution_acceptances VALUES (?,?,?,?)',
                (data['id'], data['slice_id'], data['sources']['plan']['revision'], canonical(receipt)))
            data.update(state='checkpoint', reason='Slice accepted; continuation may dispatch its successor' if data.get('continuation_id') else 'One-slice run limit reached intentionally', commit=publication['commit'],
                        last_event='checkpoint', updated_at=time.time(), blockers=[])
            db.execute('UPDATE executions SET state=?,data=?,updated_at=? WHERE id=?',
                ('checkpoint', canonical(data), data['updated_at'], data['id']))
            self.journal.event(db, data['id'], 'slice_accepted', receipt)
        from .integrated_code import reconcile
        reconcile(self.store)
        self.runtime.update(data['run_id'], 'checkpoint', 'One slice accepted; intentional checkpoint, project remains open')
