"""Durable initial architecture workflow. No slice or reevaluation loop lives here."""

from contextlib import contextmanager
import fcntl
import hashlib
import json

from .architecture_contract import validate_architecture, validate_review
from .workflow import WorkflowError

MAX_CALLS = 8  # includes failed/interrupted attempts; never resets on resume
MAX_CONTEXT_BYTES = 180_000
TRIGGERS = ('new_boundary', 'persistence_change', 'structural_runtime', 'public_protocol',
            'incompatible_requirement', 'invalidated_decision', 'repeated_architectural_failure',
            'architectural_gate')
SECTIONS = ('components', 'dependencies', 'contracts', 'data', 'integrations', 'information_flows',
            'runtime', 'security', 'observability', 'testing', 'invariants', 'constraints',
            'risks', 'assumptions', 'proposed_decisions')


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def fingerprint(value):
    return 'sha256:' + hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def migrate(db):
    for statement in (
        """CREATE TABLE IF NOT EXISTS architecture_run (
            id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS architecture_calls (
            id INTEGER PRIMARY KEY, role TEXT NOT NULL, context TEXT NOT NULL,
            response TEXT, error TEXT, status TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))""",
        """CREATE TABLE IF NOT EXISTS architecture_decisions (
            key TEXT PRIMARY KEY, decision_id INTEGER UNIQUE NOT NULL REFERENCES decisions(id),
            data TEXT NOT NULL, origin TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS architecture_baselines (
            revision INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL, architecture TEXT NOT NULL,
            source_snapshot TEXT NOT NULL, source_fingerprint TEXT NOT NULL, review TEXT NOT NULL,
            gate TEXT NOT NULL, projection TEXT NOT NULL,
            parent_revision INTEGER REFERENCES architecture_baselines(revision),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))""",
        """CREATE TABLE IF NOT EXISTS architecture_current (
            id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL REFERENCES architecture_baselines(revision))""",
        """CREATE TABLE IF NOT EXISTS architecture_adrs (
            id INTEGER PRIMARY KEY, key TEXT NOT NULL, baseline_revision INTEGER NOT NULL
            REFERENCES architecture_baselines(revision), decision TEXT NOT NULL,
            human_decision_id INTEGER REFERENCES decisions(id),
            supersedes_id INTEGER REFERENCES architecture_adrs(id),
            UNIQUE(key, baseline_revision))""",
        # Future change records must identify a real trigger/evidence and an explicit disposition.
        # No handler writes these yet; accepted changes never overwrite historical baselines/ADRs.
        f"""CREATE TABLE IF NOT EXISTS architecture_changes (
            id INTEGER PRIMARY KEY, base_revision INTEGER NOT NULL REFERENCES architecture_baselines(revision),
            trigger TEXT NOT NULL CHECK(trigger IN {TRIGGERS!r}),
            evidence TEXT NOT NULL CHECK(length(evidence)>2), proposal TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('proposed','accepted','rejected','superseded')),
            human_decision_id INTEGER REFERENCES decisions(id),
            resulting_revision INTEGER REFERENCES architecture_baselines(revision),
            affected_elements TEXT NOT NULL, rationale TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            CHECK(status != 'accepted' OR resulting_revision IS NOT NULL))""",
    ):
        db.execute(statement)
    if db.execute('PRAGMA user_version').fetchone()[0] < 3:
        db.execute('PRAGMA user_version = 3')


def read_state(db):
    result = {'stage': 'not_started', 'calls': 0, 'blockers': [], 'baseline': None, 'adrs': [],
              'decision_details': [], 'proposal': None, 'review': None}
    if db.execute('PRAGMA user_version').fetchone()[0] < 3:
        return result
    row = db.execute('SELECT data FROM architecture_run WHERE id=1').fetchone()
    if row:
        result.update(json.loads(row[0]))
    row = db.execute('''SELECT b.* FROM architecture_baselines b JOIN architecture_current c
                        ON c.revision=b.revision WHERE c.id=1''').fetchone()
    if row:
        result['baseline'] = dict(row)
        for key in ('architecture', 'source_snapshot', 'review', 'gate'):
            result['baseline'][key] = json.loads(row[key])
    result['adrs'] = [dict(row, decision=json.loads(row['decision'])) for row in db.execute(
        'SELECT * FROM architecture_adrs ORDER BY id')]
    result['decision_details'] = [dict(json.loads(row['data']), id=row['decision_id'],
                                       answer=row['answer'], origin=row['origin']) for row in db.execute(
        '''SELECT a.*, d.answer FROM architecture_decisions a JOIN decisions d
           ON d.id=a.decision_id ORDER BY d.id''')]
    return result


def source_snapshot(snapshot):
    # Active structured concepts only. Turns, messages, events and SDK threads are excluded.
    facts = [{key: item[key] for key in ('key', 'category', 'text', 'status', 'blocking', 'basis')}
             for item in snapshot['discovery']['knowledge'] if item['status'] != 'superseded']
    if not facts or any(i['status'] not in ('known', 'assumption') or i['blocking'] for i in facts):
        raise WorkflowError('Architecture needs authoritative, nonblocking discovery knowledge')
    # Preserve manual decisions created outside discovery's conversational handler too.
    known = {i['key'] for i in facts}
    for decision in snapshot['decisions']:
        key = f"human_decision_{decision['id']}"
        if decision['answer'] is not None and key not in known:
            facts.append({'key': key, 'category': 'decisions', 'text': decision['question'] + '\n' + decision['answer'],
                          'status': 'known', 'blocking': False, 'basis': 'Authoritative recorded human answer'})
    result = {'knowledge': sorted(facts, key=lambda i: i['key'])}
    if len(canonical(result).encode()) > 60_000:
        raise WorkflowError('Architecture source snapshot exceeds 60000 bytes; consolidate facts, never truncate')
    return result


def classify(source, proposal=None):
    """Conservative deterministic trivial allowlist; ambiguity always receives review."""
    facts = source['knowledge']
    text = ' '.join(i['text'].lower() for i in facts)
    signals = [word for word in ('saas', 'auth', 'autentic', 'tenant', 'multiusuario', 'payment',
                'pago', 'sensitive', 'sensible', 'distribu', 'queue', 'cola', 'asíncron', 'async',
                'million', 'millón', 'servicio', 'service') if word in text]
    critical = [word for word in ('safety-critical', 'security-critical', 'hipaa', 'pci-dss',
                'clinical', 'clínic', 'patient', 'paciente', 'bancari', 'medical', 'médic') if word in text]
    signals.extend('critical:' + word for word in critical)
    local = any(word in text for word in ('offline', 'sin red', 'local-only'))
    small = any(word in text for word in ('single user', 'un usuario', 'personal', 'trivial'))
    if any(i['category'] == 'integrations' and i['text'].lower().strip() not in
           ('none', 'ninguna', 'ninguno', 'sin integraciones') for i in facts):
        signals.append('external_integrations_or_uncertainty')
    if proposal and (len(proposal['components']) > 2 or proposal['integrations'] or
                     any(r['severity'] in ('high', 'critical') for r in proposal['risks'])):
        signals.append('structural_complexity')
    required = bool(signals or not (local and small) or len(facts) > 20)
    return {'required': required, 'risk': 'critical' if critical else 'high' if signals else 'low' if not required else 'normal',
            'reasons': signals or ['conservative_default' if required else 'small_local_single_user']}


def quality_gate(proposal, source, decisions, review_passed):
    validate_architecture(proposal)
    errors = []
    ids = [e['id'] for name in SECTIONS for e in proposal[name]]
    elements = set(ids)
    if len(ids) != len(elements):
        errors.append('duplicate_element_ids')
    components = {e['id'] for e in proposal['components']}
    contracts = {e['id']: e for e in proposal['contracts']}
    sources = {i['key']: i for i in source['knowledge']}
    answers = {d['key']: d.get('answer') for d in decisions if 'key' in d}
    accepted = {key for key, answer in answers.items() if answer and answer.strip().lower() == 'accept'}
    def require(ok, message):
        if not ok:
            errors.append(message)
    def refs(values, allowed, message, nonempty=True):
        require((bool(values) or not nonempty) and set(values) <= set(allowed), message)
    require(bool(components), 'components_missing')
    for component in proposal['components']:
        require(bool(component['responsibilities']) and bool(component['owns']), 'component_ownership:' + component['id'])
    for name in ('runtime', 'security', 'observability', 'testing', 'invariants', 'constraints'):
        require(bool(proposal[name]), name + '_missing')
        for element in proposal[name]:
            refs(element['components'], components, 'component_reference:' + element['id'])
    deployed = {c for e in proposal['runtime'] for c in e['components']}
    tested = {c for e in proposal['testing'] for c in e['components']}
    require(components <= deployed, 'runtime_coverage')
    require(components <= tested, 'testing_coverage')
    for contract in proposal['contracts']:
        refs(contract['participants'], components, 'contract_participants:' + contract['id'])
        require(bool(contract['invariants']), 'contract_invariants:' + contract['id'])
    for dep in proposal['dependencies']:
        contract = contracts.get(dep['contract'])
        require(dep['source'] in components and dep['target'] in components and dep['source'] != dep['target'],
                'dependency_endpoints:' + dep['id'])
        require(bool(contract) and {dep['source'], dep['target']} <= set(contract['participants']),
                'dependency_contract:' + dep['id'])
    require(len(components) <= 1 or bool(proposal['dependencies']), 'dependencies_missing')
    require(bool(proposal['information_flows']), 'information_flows_missing')
    edges = {(d['source'], d['target']) for d in proposal['dependencies']}
    for flow in proposal['information_flows']:
        refs(flow['steps'], components, 'flow_reference:' + flow['id'])
        require(all(a == b or (a, b) in edges or (b, a) in edges for a, b in zip(flow['steps'], flow['steps'][1:])), 'flow_dependency:' + flow['id'])
    for data in proposal['data']:
        require(data['owner'] in components and bool(data['entities']), 'data_ownership:' + data['id'])
    for integration in proposal['integrations']:
        contract = contracts.get(integration['contract'])
        require(integration['component'] in components and bool(contract) and
                integration['component'] in contract['participants'], 'integration_contract:' + integration['id'])
    coverage = {}
    for entry in proposal['coverage']:
        require(entry['source_key'] not in coverage, 'duplicate_coverage:' + entry['source_key'])
        coverage[entry['source_key']] = entry['targets']
        refs(entry['targets'], elements, 'coverage_targets:' + entry['source_key'])
        require(entry['source_key'] in sources, 'unknown_source:' + entry['source_key'])
    for key, fact in sources.items():
        require(key in coverage, 'uncovered_requirement:' + key)
        if fact['category'] == 'security':
            require(bool(set(coverage.get(key, [])) & {e['id'] for e in proposal['security']}), 'security_coverage:' + key)
        if fact['status'] == 'assumption':
            require(any(key in a['source_keys'] for a in proposal['assumptions']), 'unrecorded_assumption:' + key)
    for assumption in proposal['assumptions']:
        refs(assumption['source_keys'], sources, 'assumption_source:' + assumption['id'], nonempty=False)
    for risk in proposal['risks']:
        require(risk['severity'] != 'critical' or bool(risk['mitigation'].strip()) or
                risk['acceptance_key'] in accepted, 'critical_risk:' + risk['id'])
    require(not proposal['contradictions'], 'structural_contradictions')
    require(not proposal['unresolved_questions'], 'unresolved_questions')
    require(not any(d.get('answer') is None for d in decisions), 'pending_human_decisions')
    require(review_passed, 'review_not_passed')
    kinds = {d['kind'] for d in proposal['proposed_decisions']}
    require('system_style' in kinds, 'style_adr_missing')
    require(not any(d['persistence'] == 'persistent' for d in proposal['data']) or 'storage' in kinds, 'storage_adr_missing')
    for decision in proposal['proposed_decisions']:
        refs(decision['evidence'], sources, 'decision_evidence:' + decision['id'])
        if decision['kind'] != 'minor':
            require(bool(decision['alternatives']) and bool(decision['consequences']), 'adr_tradeoff:' + decision['id'])
        require(not decision['human_key'] or bool(answers.get(decision['human_key'])), 'decision_authority:' + decision['id'])
    for decision in decisions:
        if decision.get('origin') == 'proposal' and decision.get('answer'):
            require(any(d['human_key'] == decision['key'] for d in proposal['proposed_decisions']),
                    'human_answer_not_incorporated:' + decision['key'])
    return {'passed': not errors, 'errors': sorted(set(errors))}


def projection(architecture, revision, digest):
    lines = [f'# Architecture revision {revision}', '', digest, '',
             architecture['style']['name'], '', architecture['style']['rationale']]
    # Full lossless human projection of the structured baseline, including its coverage and rationale.
    for section, value in architecture.items():
        if section in ('schema_version', 'style'):
            continue
        lines += ['', '## ' + section.replace('_', ' ').title(), '']
        if isinstance(value, list):
            for element in value:
                if isinstance(element, dict):
                    lines += ['### ' + str(element.get('name', element.get('title', element.get('id', element.get('source_key', 'Entry'))))), '']
                    for key, detail in element.items():
                        lines.append(f'- **{key}**: ' + ('; '.join(map(str, detail)) if isinstance(detail, list) else str(detail)))
                    lines.append('')
                else:
                    lines.append('- ' + str(element))
        else:
            lines.append(str(value))
    return '\n'.join(lines) + '\n'


class Architecture:
    def __init__(self, store, model=None, router=None, should_stop=None):
        self.store, self.model, self.router = store, model, router
        self.should_stop = should_stop or (lambda: False)

    @contextmanager
    def _lock(self):
        # Process-scoped advisory lock releases on SIGKILL. No stale lease, no concurrent paid calls.
        with (self.store.path.parent / 'architecture.lock').open('a') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise WorkflowError('Architecture is already running in another process') from exc
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _save(self, db, state, revision, kind, payload=None):
        db.execute('INSERT OR REPLACE INTO architecture_run VALUES (1, ?)', (canonical(state),))
        self.store._record(db, revision + 1, kind, payload or {'stage': state['stage']})

    def _mutate(self, snapshot, state, kind, operation=None):
        with self.store._connection(write=True) as db:
            if self.store._check(db, snapshot['revision']) != 'architecture':
                raise WorkflowError('Architecture is no longer active')
            if operation:
                operation(db)
            self._save(db, state, snapshot['revision'], kind)

    def _request(self, db, question, origin):
        old = db.execute('SELECT data FROM architecture_decisions WHERE key=?', (question['key'],)).fetchone()
        if old:
            if json.loads(old[0]) != question:
                raise WorkflowError('Architecture decision key cannot change its meaning: ' + question['key'])
            return
        decision_id = db.execute('INSERT INTO decisions(question) VALUES (?)', (question['question'],)).lastrowid
        db.execute('INSERT INTO architecture_decisions VALUES (?, ?, ?, ?)',
                   (question['key'], decision_id, canonical(question), origin))

    def run(self):
        self.store.initialize()
        with self._lock():
            return self._run()

    def _run(self):
        while True:
            snapshot = self.store.snapshot()
            arch = snapshot['architecture']
            if self.should_stop():
                return snapshot
            if arch['baseline']:
                return snapshot  # completed is idempotent: never discover skills or call Codex
            if snapshot['phase'] != 'architecture':
                raise WorkflowError('Architecture can run only after discovery')
            if any(d['answer'] is None for d in snapshot['decisions']):
                return snapshot
            if arch['stage'] == 'not_started':
                source = source_snapshot(snapshot)
                state = {'stage': 'propose', 'calls': 0, 'blockers': [], 'source': source,
                         'source_fingerprint': fingerprint(source), 'proposal': None, 'review': None,
                         'reviews': [], 'reconciliations': 0, 'classification': classify(source), 'routing': None}
                self._mutate(snapshot, state, 'architecture_started')
                continue
            state = {k: v for k, v in arch.items() if k not in ('baseline', 'adrs', 'decision_details')}
            with self.store._connection() as db:
                interrupted = db.execute("SELECT id FROM architecture_calls WHERE status='pending'").fetchall()
            if interrupted:
                def abandon(db):
                    db.execute("UPDATE architecture_calls SET status='interrupted', error='Process interrupted before checkpoint' WHERE status='pending'")
                self._mutate(snapshot, state, 'architecture_interrupted_calls', abandon)
                continue
            if state['stage'] in ('blocked', 'completed'):
                return snapshot
            if state['stage'] == 'review_decisions':
                if any(d['origin'] == 'review' and d['answer'].strip().lower() != 'accept'
                       for d in arch['decision_details']):
                    state.update(stage='blocked', blockers=['Review finding rejected: baseline requires a new explicitly scoped design effort'])
                else:
                    state.update(stage='gate', blockers=[])
                self._mutate(snapshot, state, 'architecture_review_decided')
                continue
            if state['stage'] == 'gate':
                details = arch['decision_details'] + [d for d in snapshot['decisions']
                    if d['id'] not in {a['id'] for a in arch['decision_details']}]
                review_passed = not state['classification']['required'] or bool(state['reviews']) and (
                    not state['reviews'][-1]['findings'] or all(
                        any(d['key'] == 'review_' + f['id'] and d['answer'] and d['answer'].strip().lower() == 'accept'
                            for d in arch['decision_details']) for f in state['reviews'][-1]['findings']))
                gate = quality_gate(state['proposal'], state['source'], details, review_passed)
                if state['reviews']:
                    for finding in state['reviews'][-1]['findings']:
                        if finding['category'] in ('inconsistency', 'coverage'):
                            gate['errors'].append('unresolved_review_' + finding['category'] + ':' + finding['id'])
                    gate['passed'] = not gate['errors']
                state['gate'] = gate
                if not gate['passed'] and not state['reconciliations']:
                    state.update(stage='reconcile', reconciliations=1, blockers=gate['errors'])
                    self._mutate(snapshot, state, 'architecture_gate_reconciliation')
                    continue
                if not gate['passed']:
                    state.update(stage='blocked', blockers=gate['errors'])
                    self._mutate(snapshot, state, 'architecture_gate_blocked')
                    return self.store.snapshot()
                self._complete(snapshot, state, details)
                return self.store.snapshot()
            if state['calls'] >= MAX_CALLS:
                state.update(stage='blocked', blockers=[f'Architecture call limit reached ({MAX_CALLS}); no automatic retry'])
                self._mutate(snapshot, state, 'architecture_budget_exhausted')
                return self.store.snapshot()
            self._call(snapshot, state)

    def _call(self, snapshot, state):
        from .skill_catalog import discover_catalog
        from .skill_router import SkillRouter, Work, load_config
        role = state['stage']
        try:
            if self.router is None:
                roots, policy = load_config(self.store.project)
                catalog = discover_catalog(self.store.project, roots)
                if catalog.errors:
                    raise WorkflowError('; '.join(catalog.errors))
                self.router = SkillRouter(catalog, policy)
            concerns = ('security',) if any(i['category'] == 'security' for i in state['source']['knowledge']) else ()
            routing = self.router.route(Work(domain='architecture', intent='review' if role.startswith('critic') else 'design',
                                            risk=state['classification']['risk'], concerns=concerns))
            skill_inputs = routing.required_inputs(self.router.catalog)
            state['routing'] = routing.as_dict()
            context = {'role': role, 'source': state['source'], 'source_fingerprint': state['source_fingerprint'],
                       'proposal': state['proposal'], 'review': state['review'],
                       'human_answers': snapshot['architecture']['decision_details'],
                       'other_human_answers': [d for d in snapshot['decisions'] if d['answer'] is not None and
                           d['id'] not in {a['id'] for a in snapshot['architecture']['decision_details']}],
                       'gate_errors': state.get('gate', {}).get('errors', []),
                       'skills': routing.context(), 'limits': {'critic_passes': 2, 'reconciliations': 1}}
            if len(canonical(context).encode()) > MAX_CONTEXT_BYTES:
                raise WorkflowError('Architecture context budget exceeded; no facts were truncated')
        except WorkflowError as exc:
            state['blockers'] = [str(exc)]
            self._mutate(snapshot, state, 'architecture_preflight_failed')
            raise
        state['calls'] += 1
        state['blockers'] = []
        with self.store._connection(write=True) as db:
            self.store._check(db, snapshot['revision'])
            call_id = db.execute("INSERT INTO architecture_calls(role, context, status) VALUES (?, ?, 'pending')",
                                 (role, canonical({'input': context, 'required_skills': skill_inputs}))).lastrowid
            self._save(db, state, snapshot['revision'], 'architecture_call_started', {'call_id': call_id, 'role': role})
        revision = snapshot['revision'] + 1
        try:
            if self.model is None:
                from .codex_architecture import CodexArchitecture
                self.model = CodexArchitecture()
            response = self.model.respond(context, skill_inputs=skill_inputs)
            (validate_review if role.startswith('critic') else validate_architecture)(response)
            if len(canonical(response).encode()) > 110_000:
                raise WorkflowError('Architecture output exceeds 110000 bytes; consolidate the baseline')
            with self.store._connection(write=True) as db:
                self.store._check(db, revision)
                db.execute("UPDATE architecture_calls SET response=?, status='completed' WHERE id=?",
                           (canonical(response), call_id))
                if role.startswith('critic'):
                    review_targets = {e['id'] for name in SECTIONS for e in state['proposal'][name]} | {
                        f['key'] for f in state['source']['knowledge']}
                    if any(not f['targets'] or not set(f['targets']) <= review_targets for f in response['findings']):
                        raise WorkflowError('Critic findings need valid architecture/source references')
                    if len({f['id'] for f in response['findings']}) != len(response['findings']):
                        raise WorkflowError('Duplicate critic finding IDs')
                    state['review'] = response
                    state['reviews'].append(response)
                    if response['findings'] and role == 'critic':
                        state.update(stage='reconcile', reconciliations=1)
                    elif response['findings']:
                        state['stage'] = 'review_decisions'
                        for finding in response['findings']:
                            # Controller-owned keys are deterministic and never model-authored.
                            key = 'review_' + finding['id']
                            self._request(db, {'key': key, 'question': finding['description'],
                                'options': ['accept', 'reject'] if finding['category'] in ('risk', 'complexity') else ['reject'], 'recommendation': finding['recommendation'],
                                'consequences': ['accept: accepts residual risk/complexity only; coverage gaps and contradictions cannot be waived',
                                                 'reject: block the baseline without further automatic review']}, 'review')
                    else:
                        state['stage'] = 'gate'
                else:
                    state['proposal'] = response
                    if any(q['key'].startswith('review_') for q in response['unresolved_questions']):
                        raise WorkflowError('The review_ decision namespace belongs to the controller')
                    for question in response['unresolved_questions']:
                        self._request(db, question, 'proposal')
                    # Human answers resume the same author role; the single reconciliation is
                    # a logical pass that may pause for input, bounded by the global call budget.
                    if not response['unresolved_questions']:
                        classification = classify(state['source'], response)
                        if state['classification']['required']:
                            if not classification['required']:
                                classification['reasons'].append('previously_required_review')
                            classification['required'] = True  # simplification never bypasses an outstanding review
                            risks = ('low', 'normal', 'high', 'critical')
                            classification['risk'] = max((classification['risk'], state['classification']['risk']), key=risks.index)
                        state['classification'] = classification
                        state['stage'] = ('critic_final' if role == 'reconcile' else 'critic') if state['classification']['required'] else 'gate'
                self._save(db, state, revision, 'architecture_call_completed', {'call_id': call_id, 'role': role})
        except BaseException as exc:
            # Even Ctrl-C is durable. Failed/stale output cannot overwrite current state.
            with self.store._connection(write=True) as db:
                current = db.execute('SELECT revision FROM workflow WHERE id=1').fetchone()[0]
                db.execute("UPDATE architecture_calls SET error=?, status='failed' WHERE id=?", (str(exc), call_id))
                if current == revision:
                    saved = json.loads(db.execute('SELECT data FROM architecture_run WHERE id=1').fetchone()[0])
                    saved['blockers'] = [str(exc) or type(exc).__name__]
                    self._save(db, saved, current, 'architecture_call_failed', {'call_id': call_id})
                else:
                    self.store._record(db, current + 1, 'architecture_stale_call', {'call_id': call_id})
            raise

    def _complete(self, snapshot, state, details):
        architecture = state['proposal']
        digest = fingerprint(architecture)
        revision = 1  # initial baseline only; future revisions require an accepted change record
        human = {d['key']: d['id'] for d in details if 'key' in d}
        review = {'classification': state['classification'], 'passes': state['reviews'],
                  'human_decisions': details, 'limits': {'critic_passes': 2, 'reconciliations': 1, 'calls': MAX_CALLS}}
        with self.store._connection(write=True) as db:
            self.store._check(db, snapshot['revision'])
            db.execute('''INSERT INTO architecture_baselines(revision, fingerprint, architecture,
                source_snapshot, source_fingerprint, review, gate, projection) VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                (revision, digest, canonical(architecture), canonical(state['source']), state['source_fingerprint'],
                 canonical(review), canonical(state['gate']), projection(architecture, revision, digest)))
            db.execute('INSERT INTO architecture_current VALUES (1, ?)', (revision,))
            for decision in architecture['proposed_decisions']:
                if decision['kind'] != 'minor':
                    db.execute('''INSERT INTO architecture_adrs(key, baseline_revision, decision, human_decision_id)
                                  VALUES (?, ?, ?, ?)''',
                               (decision['id'], revision, canonical(decision), human.get(decision['human_key'])))
            state.update(stage='completed', blockers=[])
            db.execute("UPDATE workflow SET phase='planning' WHERE id=1")
            self._save(db, state, snapshot['revision'], 'architecture_completed',
                       {'from': 'architecture', 'to': 'planning', 'revision': revision, 'fingerprint': digest})
