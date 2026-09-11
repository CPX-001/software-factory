"""Project-specific, revisable checkpoints on the existing Store and detached Runtime.

No phase engine or second worker transport: Controller owns the loop, CodexExecution
owns streaming/interruption/recovery, and this journal records each useful step.
"""
from copy import deepcopy
import json
import subprocess
import time
import uuid

from .registry import FactoryError
from .runtime import ACTIVE, Runtime, read_state


def enabled(store):
    if not store.path.is_file():
        return False
    with store._connection() as db:
        return bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='adaptive_project'").fetchone())


class Adaptive:
    def __init__(self, store):
        self.store = store

    def initialize(self):
        with self.store._connection(write=True) as db:
            db.execute('CREATE TABLE IF NOT EXISTS adaptive_project(id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL)')
            db.execute('''CREATE TABLE IF NOT EXISTS adaptive_inputs(id INTEGER PRIMARY KEY,
                request_id TEXT UNIQUE NOT NULL, message TEXT NOT NULL, step_id INTEGER)''')
            db.execute('''CREATE TABLE IF NOT EXISTS adaptive_steps(id INTEGER PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES factory_runs(id), state TEXT NOT NULL, data TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS adaptive_questions(id INTEGER PRIMARY KEY,
                step_id INTEGER NOT NULL REFERENCES adaptive_steps(id), question TEXT NOT NULL,
                reason TEXT NOT NULL, status TEXT NOT NULL, answer TEXT)''')
            initial = {'focus': 'idea', 'status': 'waiting_for_user', 'objective': '', 'memory': '',
                       'message': 'Describe tu idea y lo que quieres conseguir.', 'next_step': '',
                       'tasks': [], 'checks': [], 'questions': [], 'documents': [], 'deferred': [], 'revisions': [],
                       'revision': 0, 'settings': {}}
            db.execute('INSERT OR IGNORE INTO adaptive_project VALUES (1,?)', (json.dumps(initial),))
            db.execute("UPDATE workflow SET phase='adaptive' WHERE id=1")

    def state(self, db=None):
        if db is not None:
            return json.loads(db.execute('SELECT data FROM adaptive_project WHERE id=1').fetchone()[0])
        with self.store._connection() as db:
            return self.state(db)

    def _save(self, db, state):
        db.execute('UPDATE adaptive_project SET data=? WHERE id=1', (json.dumps(state, ensure_ascii=False),))

    def pending(self, db):
        return [dict(row) for row in db.execute('SELECT id,message FROM adaptive_inputs WHERE step_id IS NULL ORDER BY id')]

    def questions(self):
        with self.store._connection() as db:
            return [dict(r) for r in db.execute("SELECT * FROM adaptive_questions WHERE status='pending' ORDER BY id")]

    def enqueue(self, message, request_id=None):
        if not isinstance(message, str) or not message.strip():
            raise FactoryError('invalid_message', 'A nonempty project message is required')
        with self.store._connection(write=True) as db:
            key = request_id or str(uuid.uuid4())
            row = db.execute('SELECT message FROM adaptive_inputs WHERE request_id=?', (key,)).fetchone()
            if row:
                if row[0] != message:
                    raise FactoryError('request_id_conflict', 'Request ID already used for different input')
                return False
            db.execute('INSERT INTO adaptive_inputs(request_id,message) VALUES (?,?)', (key, message))
            Runtime.event(db, 'project_input', {'request_id': key})
        return True

    def answer(self, decision_id, answer):
        with self.store._connection(write=True) as db:
            row = db.execute('SELECT * FROM adaptive_questions WHERE id=?', (decision_id,)).fetchone()
            if not row:
                raise FactoryError('decision_missing', 'Unknown project question')
            if row['status'] == 'answered' and row['answer'] == answer:
                return False
            if row['status'] != 'pending':
                raise FactoryError('decision_stale', 'This question has already been answered or revised')
            db.execute("UPDATE adaptive_questions SET answer=?,status='answered' WHERE id=?", (answer, decision_id))
            db.execute('INSERT INTO adaptive_inputs(request_id,message) VALUES (?,?)',
                       ('answer-' + str(decision_id), row['question'] + '\nUser answer: ' + answer))
        return True

    def ready(self):
        with self.store._connection() as db:
            return bool(self.pending(db) or self.state(db)['status'] == 'continue' or
                        db.execute("SELECT 1 FROM adaptive_steps WHERE state!='applied' AND state!='interrupted'").fetchone())

    def configure(self, settings):
        allowed = {'model', 'effort', 'service_tier', 'max_calls', 'max_tokens', 'max_seconds'}
        if not isinstance(settings, dict) or set(settings) - allowed:
            raise FactoryError('invalid_settings', 'Unknown Codex project setting')
        for key, value in settings.items():
            if key.startswith('max_'):
                if value is not None and (type(value) is not int or value <= 0):
                    raise FactoryError('invalid_settings', 'Optional limits must be positive integers or null')
            elif value is not None and (not isinstance(value, str) or not value.strip()):
                raise FactoryError('invalid_settings', 'Model settings must be strings or null (inherit Codex)')
        if settings.get('effort'):
            from openai_codex.generated.v2_all import ReasoningEffort
            try:
                ReasoningEffort(settings['effort'])
            except ValueError as exc:
                raise FactoryError('invalid_settings', 'Unknown reasoning effort') from exc
        with self.store._connection(write=True) as db:
            state = self.state(db)
            state['settings'].update(settings)
            self._save(db, state)
            Runtime.event(db, 'project_settings', settings)
        return state['settings']

    def steps(self, db):
        return [dict(json.loads(r['data']), id=r['id'], state=r['state'], run_id=r['run_id'])
                for r in db.execute('SELECT * FROM adaptive_steps ORDER BY id')]

    def usage(self):
        with self.store._connection() as db:
            steps = self.steps(db)
        # Each fresh step uses a fresh thread. Interrupted retries stay on that step.
        tokens = 0
        for step in steps:
            for attempt in step.get('attempts', []):
                usage = attempt.get('usage') or {}
                total = usage.get('total', usage)
                tokens += total.get('totalTokens', total.get('total_tokens', 0)) or 0
        return {'calls': sum(s.get('calls', 0) for s in steps), 'call_unit': 'codex_turns', 'tokens': tokens,
                'seconds': sum(s.get('seconds', 0) for s in steps),
                'usage_unreported': sum(not a.get('usage') for s in steps for a in s.get('attempts', []))}

    def limit(self, *, new_call=False):
        settings, usage = self.state()['settings'], self.usage()
        if new_call and settings.get('max_tokens') and usage['usage_unreported']:
            return 'project_usage_unknown'
        for field in ('calls', 'tokens', 'seconds'):
            limit = settings.get('max_' + field)
            if limit and usage[field] >= limit and (field != 'calls' or new_call):
                return 'project_' + field + '_limit'
        return None

    def _owner(self, db, run_id):
        runtime = read_state(db)
        if runtime['run_id'] != run_id or runtime['status'] not in ACTIVE:
            raise FactoryError('stale_run', 'Another run owns this project')

    def prepare(self, run_id):
        with self.store._connection(write=True) as db:
            self._owner(db, run_id)
            old = db.execute("SELECT * FROM adaptive_steps WHERE state NOT IN ('applied','interrupted') ORDER BY id DESC LIMIT 1").fetchone()
            if old:
                return dict(json.loads(old['data']), id=old['id'], state=old['state'])
            state = self.state(db)
            inputs = self.pending(db)
            context = {'project_path': str(self.store.project), 'checkpoint': {k: v for k, v in state.items()
                       if k not in ('settings', 'delivery', 'version')}, 'user_messages': inputs}
            data = {'context': context, 'calls': 0, 'attempts': [], 'seconds': 0, 'created_at': time.time(), 'tools': []}
            cursor = db.execute("INSERT INTO adaptive_steps(run_id,state,data) VALUES (?,'prepared',?)", (run_id, json.dumps(data)))
            return {**data, 'id': cursor.lastrowid, 'state': 'prepared'}

    def save_step(self, step, run_id):
        with self.store._connection(write=True) as db:
            self._owner(db, run_id)
            db.execute('UPDATE adaptive_steps SET state=?,data=? WHERE id=?',
                       (step['state'], json.dumps(step), step['id']))

    def apply(self, step, run_id):
        from jsonschema import Draft202012Validator
        from .codex_adaptive import CHECKPOINT
        response = deepcopy(step['result']['response'])
        errors = list(Draft202012Validator(CHECKPOINT).iter_errors(response))
        if errors:
            raise FactoryError('invalid_checkpoint', errors[0].message[:500])
        if len(response['memory']) > 12000:
            raise FactoryError('invalid_checkpoint', 'Project memory exceeds 12000 characters; move detail to documents')
        with self.store._connection(write=True) as db:
            self._owner(db, run_id)
            row = db.execute('SELECT state FROM adaptive_steps WHERE id=?', (step['id'],)).fetchone()
            if row[0] == 'applied':
                return
            previous = self.state(db)
            # Required checks are chosen by Codex, not imposed by Factory. Once chosen,
            # a later checkpoint cannot silently erase them in order to finish.
            by_id = {c['id']: c for c in response['checks']}
            revised = {r['check_id'] for r in response['revisions'] if r['reason'].strip()}
            for check in previous['checks']:
                changed = check['id'] not in by_id or any(by_id[check['id']][key] != check[key] for key in ('description', 'required'))
                if check['required'] and changed and check['id'] not in revised:
                    response['checks'] = [c for c in response['checks'] if c['id'] != check['id']]
                    response['checks'].append(check)
                    response['status'] = 'continue'
                    response['next_step'] = 'Resolve the previously committed check ' + check['id'] + ', or record the substantive reason for revising it. It cannot silently disappear.'
            incomplete = [c['id'] for c in response['checks'] if c['required'] and
                          (c['status'] != 'passed' or not c['evidence'].strip())]
            if response['status'] == 'completed' and (incomplete or response['questions'] or
                    any(t['status'] in ('pending', 'active') for t in response['tasks'])):
                response['status'] = 'waiting_for_user' if response['questions'] else 'continue'
                response['next_step'] = 'Finish the remaining agreed work/checks, or state the concrete external blocker: ' + ', '.join(incomplete)
            if response['status'] == 'waiting_for_user' and not response['questions']:
                response['questions'] = [{'question': response['message'], 'reason': 'Input needed to continue'}]
            response.update(revision=previous['revision'] + 1, settings=previous['settings'])
            response['version'] = step.get('version')
            self._save(db, response)
            for item in step['context']['user_messages']:
                db.execute('UPDATE adaptive_inputs SET step_id=? WHERE id=? AND step_id IS NULL', (step['id'], item['id']))
            db.execute("UPDATE adaptive_questions SET status='superseded' WHERE status='pending'")
            for q in response['questions']:
                db.execute("INSERT INTO adaptive_questions(step_id,question,reason,status) VALUES (?,?,?,'pending')",
                           (step['id'], q['question'], q['reason']))
            step['state'] = 'applied'
            db.execute("UPDATE adaptive_steps SET state='applied',data=? WHERE id=?", (json.dumps(step), step['id']))
            revision = db.execute('SELECT revision FROM workflow WHERE id=1').fetchone()[0] + 1
            self.store._record(db, revision, 'project_checkpoint', {'step_id': step['id'], 'focus': response['focus'], 'status': response['status']})

    def project_version(self):
        def git(*args):
            result = subprocess.run(['git', '-C', str(self.store.project), *args], capture_output=True, text=True, timeout=10)
            return result.stdout.strip() if result.returncode == 0 else None
        return {'head': git('rev-parse', 'HEAD'), 'working_tree': git('status', '--short', '--', '.', ':!.factory')}

    def report(self):
        state = self.state()
        if state['status'] != 'completed':
            return None
        directory = self.store.path.parent / 'deliveries' / ('checkpoint-' + str(state['revision']))
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / 'REPORT.md'
        text = '# ' + (state['objective'] or self.store.project.name) + '\n\n' + state['message']
        text += '\n\nCódigo: ' + str(self.store.project) + '\n\nEstado: completed (evaluación de Codex; sin certificación independiente de Factory).'
        text += '\n\nVersión observada: ' + json.dumps(state.get('version'), ensure_ascii=False)
        for title, items in [('Trabajo', [t['title'] + ': ' + t['status'] for t in state['tasks']]),
                             ('Comprobaciones reportadas por Codex', [c['description'] + ': ' + c['status'] + '. ' + c['evidence'] for c in state['checks']]),
                             ('Documentación y procedimientos', state['documents']), ('Diferido', state['deferred'])]:
            text += '\n\n## ' + title + '\n\n' + ('\n'.join('- ' + item for item in items) or 'No especificado.')
        temp = path.with_suffix('.tmp')
        temp.write_text(text + '\n')
        temp.replace(path)
        return str(path)

    def status(self, target):
        state, runtime = self.state(), Runtime(self.store).state()
        questions = self.questions()
        label = 'pause_requested' if runtime['paused'] and runtime['status'] in ACTIVE else 'paused' if runtime['paused'] else (
            'working' if runtime['status'] in ACTIVE else runtime['status'] if runtime['status'] in ('failed', 'interrupted', 'blocked') else state['status'])
        path = self.store.path.parent / 'deliveries' / ('checkpoint-' + str(state['revision'])) / 'REPORT.md'
        return {'project': target, 'workflow': 'adaptive', 'phase': state['focus'], 'state': label,
                'workflow_revision': state['revision'], 'current_objective': state['objective'], 'last_message': state['message'],
                'questions': questions, 'pending_decisions': {'count': len(questions), 'items': questions[:3]},
                'blockers': [runtime['reason']] if runtime['status'] in ('failed', 'blocked', 'interrupted') else [],
                'next_action': {'action': 'get_status' if runtime['status'] in ACTIVE else 'resume' if runtime['paused'] else
                                'done' if state['status'] == 'completed' and not self.ready() else 'answer' if questions else 'message',
                                'reason': state['next_step']},
                'tasks': state['tasks'], 'checks': state['checks'], 'deferred': state['deferred'],
                'settings': state['settings'], 'usage': self.usage(),
                'autonomous_run': {k: runtime.get(k) for k in ('run_id', 'status', 'paused', 'reason')},
                'completion': {'status': 'completed' if state['status'] == 'completed' else 'pending', 'version': state.get('version'),
                               'delivery': str(path) if path.is_file() else None, 'independently_verified': False}}


def step(service, journal, run_id, worker):
    """One recoverable unit; the existing Controller decides whether to take another."""
    from .execution_sandbox import is_alive
    data = journal.prepare(run_id)
    def publish():
        try:
            journal.apply(data, run_id)
        except FactoryError as exc:
            if exc.code != 'invalid_checkpoint' or len(data.get('rejected_results', [])) >= 2:
                raise
            # Repair the continuity record, not the product again. Keep the original
            # output and spent calls durable; never strand recovery on an invalid cache.
            data.setdefault('rejected_results', []).append(data.pop('result'))
            data['context']['checkpoint_repair'] = {'error': str(exc),
                'previous_response': data['rejected_results'][-1]['response'],
                'instruction': 'Repair only the checkpoint format. Inspect current files if needed; do not repeat completed work.'}
            data.pop('runtime', None)
            data['state'] = 'prepared'
            journal.save_step(data, run_id)
    if data.get('result'):
        publish()
        return
    if data.get('runtime'):
        if is_alive(data['runtime'].get('process')):
            raise FactoryError('previous_worker_alive', 'The previous Codex process is still active; no overlapping execution')
        recovered = worker.recover(data['runtime'])
        if recovered:
            data.update(result=recovered, state='result_saved', version=journal.project_version())
            journal.save_step(data, run_id)
            publish()
            return
        # Preserve partial edits and explain them to a fresh step on recovery.
        data['context']['recovery'] = 'The prior turn was interrupted. Inspect current files and continue without repeating completed work.'
    limit = journal.limit(new_call=True)
    if limit:
        raise FactoryError(limit, 'The optional cumulative project limit was reached')
    started = time.monotonic()
    data.update(state='running', calls=data.get('calls', 0) + 1)
    data['attempts'].append({})
    journal.save_step(data, run_id)
    def save(key, value):
        data[key] = value
        if key in ('runtime', 'usage'):
            data['attempts'][-1][key] = value
        data['seconds'] = elapsed + time.monotonic() - started
        journal.save_step(data, run_id)
    elapsed = data.get('seconds', 0)
    def should_stop():
        if Runtime(journal.store).paused():
            return 'paused'
        settings = journal.state()['settings']
        seconds = settings.get('max_seconds')
        if seconds and journal.usage()['seconds'] + time.monotonic() - started - (data['seconds'] - elapsed) >= seconds:
            return 'project_seconds_limit'
        return journal.limit()
    worker.on_item = lambda value: save('tools', [*data['tools'], value])
    try:
        result = worker.respond(data['context'], thread_id=None, should_stop=should_stop,
            on_runtime=lambda value: save('runtime', value), on_usage=lambda value: save('usage', value), on_quota=lambda value: None)
        data.update(result=result, state='result_saved', version=journal.project_version())
        # The result is durable before updating the project checkpoint or writing a report.
        save('seconds', elapsed + time.monotonic() - started)
        publish()
    except Exception as exc:
        save('error', {'code': getattr(exc, 'code', 'worker_failed'), 'message': str(exc)[:1000]})
        raise
