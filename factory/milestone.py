"""Milestone gate used by Continuation; no model or independent orchestration loop."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import time

from .architecture import canonical, fingerprint
from .continuation_store import check_budget
from .execution import Execution, current_sources
from .execution_sandbox import LinuxSandbox
from .execution_workspace import code_identity, files, git, prepare, tree_object
from .integrated_code import REF, reconcile
from .milestone_store import MilestoneStore, fence
from .registry import FactoryError
from .runtime import Runtime
from .verification import Verifier, capability_errors, protected_harness


def criteria(milestone):
    return milestone['success_criteria'] + milestone['closure_conditions']


def closure_gates(plan, mid):
    return [g for g in plan['gates'] if g['target'] == mid and g['trigger'] in ('milestone_close', 'project_checkpoint')]


def eligible_milestone(plan, closed):
    return next((m for m in plan['milestones'] if m['id'] not in closed and set(m['dependencies']) <= set(closed)), None)


def remediation_view(plan, definition, unit):
    """Trusted projection of approved gates onto a separate execution unit.

    Original slices/criteria/checks are never rewritten in planning or receipts.
    Include accepted slice regressions and all mechanical closure gates for this milestone.
    """
    plan, definition = deepcopy(plan), deepcopy(definition)
    spec = unit['remediation']
    selected = unit['slice']
    project = spec.get('scope') == 'project'
    members = {s['id'] for s in plan['slices'] if project or s['milestone'] == selected['milestone']}
    gates = [g for g in plan['gates'] if (g['target'] in members and g['trigger'] == 'after_slice') or
             ((project or g['target'] == selected['milestone']) and g['trigger'] in ('milestone_close', 'project_checkpoint', 'project_close'))]
    ids = {g['id'] for g in gates}
    checks = []
    for c in definition['checks']:
        if c['gate'] not in ids or c['kind'] == 'human_review':
            continue
        if c['id'] in spec['checks']:
            c['criteria'] = spec['criteria_checks'][c['id']]
        else:
            c['criteria'] = []
        checks.append(c)
    # Human checks remain closure obligations, not worker acceptance criteria.
    for g in gates:
        g.update(target=selected['id'], trigger='after_slice')
        mapped = sorted({i for c in checks if c['gate'] == g['id'] for i in c['gate_checks']})
        g['checks'] = [g['checks'][i] for i in mapped]
        for c in checks:
            if c['gate'] == g['id']:
                c['gate_checks'] = [mapped.index(i) for i in c['gate_checks']]
    plan['gates'] = [g for g in gates if g['checks']]
    plan['slices'].append(selected)
    definition['checks'] = checks
    return plan, definition


class MilestoneGate:
    scope = 'milestone'
    directory = 'milestones'

    def __init__(self, controller, group, snapshot, definition):
        self.controller, self.group = controller, group
        self.store, self.service = controller.store, controller.service
        self.journal = MilestoneStore(self.store)
        self.snapshot, self.plan, self.definition = snapshot, snapshot['planning']['roadmap']['plan'], definition
        self.milestone = next(m for m in self.plan['milestones'] if m['id'] == group['milestone'])

    def closed_receipt(self):
        return self.journal.closed(self.group['sources']).get(self.milestone['id'])

    def prepare_candidate(self, data, root):
        prepare(self.store, data)
        identity = code_identity(data['worktree'])
        if (git(data['worktree'], 'rev-parse', 'HEAD') != data['binding']['commit'] or
                git(data['worktree'], 'status', '--porcelain', '--untracked-files=all') or
                tree_object(data['worktree'], root / 'candidate.index') != git(data['worktree'], 'rev-parse', data['binding']['commit'] + '^{tree}') or
                ('code_id' in data and data['code_id'] != identity)):
            raise FactoryError('stale_evidence', 'Validation workspace no longer matches the exact accepted candidate')
        data['code_id'] = identity

    def members(self):
        return [s for s in self.plan['slices'] if s['milestone'] == self.milestone['id']]

    def stop_reason(self):
        if self.controller.runtime.paused():
            return 'paused'
        with self.store._connection() as db:
            try:
                fence(db, self.group)
                check_budget(db, self.group['id'])
            except FactoryError as exc:
                return exc.code
        return False

    def check_running(self):
        reason = self.stop_reason()
        if reason:
            raise FactoryError(reason, 'Pause, ownership or persistent run budget prevents milestone work')

    def binding(self):
        snapshot, sources, _, policy = Execution(self.service, self.store).prerequisites(check_selected=False)
        if sources != self.group['sources'] or policy != self.group['policy']:
            raise FactoryError('stale_sources', 'Milestone commitments/authorization changed')
        with self.store._connection() as db:
            review_ids = {r[0] for r in db.execute('SELECT decision_id FROM milestone_reviews')}
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='project_reviews'").fetchone():
                review_ids.update(r[0] for r in db.execute('SELECT decision_id FROM project_reviews'))
        return {'sources': sources, 'definition_id': policy['definition_id'],
                'commit': reconcile(self.store)['commit'],
                'decisions': [d for d in snapshot['decisions'] if d['id'] not in review_ids],
                'adrs': snapshot['architecture']['adrs'],
                'runner_sha256': hashlib.sha256(Path(__file__).with_name('verification_runner.py').read_bytes()).hexdigest(),
                'python_sha256': hashlib.sha256(Path('/usr/bin/python3').read_bytes()).hexdigest()}

    def obligations(self):
        mid = self.milestone['id']
        gates = closure_gates(self.plan, mid)
        ids = {g['id'] for g in gates}
        checks = [c for c in self.definition['checks'] if c['gate'] in ids]
        errors = []
        for g in gates:
            covered = {i for c in checks if c['gate'] == g['id'] for i in c['gate_checks']}
            if covered != set(range(len(g['checks']))):
                errors.append('verification_definition_missing:' + g['id'])
        covered = {i for c in checks for i in c['criteria']}
        if covered != set(range(len(criteria(self.milestone)))):
            errors.append('milestone_criteria_unmapped')
        human = {i for c in checks if c['kind'] == 'human_review' for i in c['criteria']}
        if not set(self.milestone.get('subjective_criteria', [])) <= human:
            errors.append('subjective_criteria_require_human_review')
        if not gates or not set(self.milestone['verification_gates']) <= ids:
            errors.append('milestone_gates_missing')
        return gates, checks, errors

    def review(self, validation, check):
        with self.store._connection(write=True) as db:
            fence(db, self.group)
            row = db.execute('SELECT decision_id FROM ' + self.scope + '_reviews WHERE validation_id=? AND check_id=?',
                             (validation['id'], check['id'])).fetchone()
            if row:
                decision_id = row[0]
            else:
                question = ('Review ' + self.scope + ' ' + self.milestone['id'] + ' at commit ' + validation['binding']['commit'] +
                    ', validation ' + validation['id'] + '. ' + check['target'] + '\nCriteria: ' +
                    canonical([criteria(self.milestone)[i] for i in check['criteria']]) +
                    '\nAnswer accept only if this exact candidate satisfies the review. Otherwise answer ambiguous, architecture_change, scope_change or provide the pending decision/diagnosis.')
                decision_id = db.execute('INSERT INTO decisions(question) VALUES (?)', (question,)).lastrowid
                db.execute('INSERT INTO ' + self.scope + '_reviews VALUES (?,?,?)', (validation['id'], check['id'], decision_id))
                Runtime.event(db, self.scope + '_review_requested', {'validation': validation['id'], 'decision_id': decision_id})
            decision = dict(db.execute('SELECT * FROM decisions WHERE id=?', (decision_id,)).fetchone())
        return {'check_id': check['id'], 'gate': check['gate'], 'criteria': check['criteria'],
                'gate_checks': check['gate_checks'], 'code_id': validation['code_id'],
                'commit': validation['binding']['commit'], 'decision': decision,
                'status': 'PASS' if (decision['answer'] or '').strip().lower() == 'accept' else 'NOT_RUN',
                'reason': None if (decision['answer'] or '').strip().lower() == 'accept' else 'human_review_pending',
                'classification': {'ambiguous': 'ambiguous_criterion', 'architecture_change': 'architecture_change',
                                   'scope_change': 'scope_decision'}.get((decision['answer'] or '').strip().lower(), 'decision_pending')}

    def criterion_status(self, checks, evidence):
        passed = {e['check_id'] for e in evidence if e['status'] == 'PASS'}
        result = []
        for i, text in enumerate(criteria(self.milestone)):
            required = [c['id'] for c in checks if i in c['criteria']]
            result.append({'index': i, 'text': text, 'checks': required,
                           'status': 'PASS' if required and set(required) <= passed else 'PENDING'})
        return result

    def run(self):
        closed = self.closed_receipt()
        if closed:
            return closed
        self.check_running()
        gates, checks, errors = self.obligations()
        self.group.update(state=self.scope + '_ready', active_gate=None,
            pending_gates=[{'id': g['id'], 'kind': g['kind'], 'trigger': g['trigger'], 'status': 'NOT_RUN'} for g in gates])
        self.controller.journal.save(self.group)
        if errors:
            raise FactoryError('validation_pending', 'Milestone criteria/gates lack authorized verification; no PASS inferred',
                               details={'classification': 'unsupported_validation', 'pending': errors})
        binding = self.binding()
        identifier = fingerprint({'milestone': self.milestone, 'binding': binding})
        saved = next((v for v in self.journal.rows('milestone_validations') if v['id'] == identifier), None)
        root = self.store.path.parent / self.directory / identifier
        data = saved or {'id': identifier, 'milestone': self.milestone['id'], 'binding': binding,
            'repository': self.group['policy']['repository'], 'base_commit': binding['commit'],
            'branch': 'factory/validate-' + identifier.split(':')[-1], 'worktree': str(root / 'worktree'),
            'state': 'validating', 'evidence': [], 'created_at': time.time()}
        data['criteria'] = self.criterion_status(checks, data['evidence'])
        self.group.update(validation_id=identifier, validation_scope=self.scope, state=self.scope + '_validating')
        self.group['candidate_commit'] = binding['commit']
        self.controller.journal.save(self.group)
        sandbox = LinuxSandbox(root / 'runtime')
        if sandbox.live():
            raise FactoryError('validation_runtime_alive', 'Existing validation sandbox is alive; no duplicate checks')
        (sandbox.directory / 'pause').unlink(missing_ok=True)
        sandbox.lifetime = max(.01, self.group['deadline'] - time.time())
        self.prepare_candidate(data, root)
        self.journal.save_validation(self.group, data)
        # Capability and harness checks use exactly the strategic gates selected by planning.
        target = {'id': self.milestone['id'], 'scope': [], 'verification_expectation': '',
                  'components': sorted({c for s in self.members() for c in s['components']})}
        projected = {**self.plan, 'gates': [{**g, 'trigger': 'after_slice'} for g in gates]}
        unsupported = capability_errors(projected, target, self.definition, self.group['policy'],
                                        self.snapshot['architecture']['baseline']['architecture'])
        if unsupported:
            data.update(state='blocked', diagnostic={'classification': 'unsupported_validation', 'pending': unsupported})
            self.journal.save_validation(self.group, data)
            raise FactoryError('capability_unavailable', 'Mandatory closure/checkpoint capability is unsupported', details=data['diagnostic'])
        required = {h for g in gates for h in g['harness']}
        locations = {h['id']: h['paths'] for h in self.definition['harness']}
        inventory = files(data['worktree'])
        if any(not locations.get(h) or any(p not in inventory for p in locations[h]) for h in required):
            raise FactoryError('validation_pending', 'Required closure harness is unavailable', details={'classification': 'infrastructure'})
        # All previously frozen oracles remain protected across slices and remediations.
        for receipt in self.controller.executions.acceptances():
            frozen = self.controller.executions.definition(receipt.get('harness_revision'))
            if receipt['sources'] == self.group['sources'] and frozen and any(inventory.get(p) != v for p, v in frozen['files'].items()):
                raise FactoryError('verification_weakened', 'Accepted frozen harness changed; closure cannot weaken its criteria')
        verifier = Verifier(sandbox, clean=self.scope == 'project')
        probed = False
        for check in checks:
            self.check_running()
            previous = next((e for e in data['evidence'] if e['check_id'] == check['id']), None)
            artifact = root / 'checks' / (check['id'] + '.json')
            if previous is None and artifact.is_file() and check['kind'] != 'human_review':
                saved_check = json.loads(artifact.read_text())
                if saved_check['binding'] == binding and saved_check['definition'] == check:
                    previous = saved_check['evidence']
                    if previous['code_id'] != data['code_id']:
                        raise FactoryError('stale_evidence', 'Recovered check belongs to different code')
                    data['evidence'].append(previous)
            if previous and previous['status'] in ('PASS', 'FAIL') and check['kind'] != 'human_review':
                continue
            self.group['active_gate'] = check['gate']
            self.controller.journal.save(self.group)
            if check['kind'] == 'human_review':
                evidence = self.review(data, check)
            else:
                if not probed:
                    sandbox.probe(); probed = True
                gate = next(g for g in gates if g['id'] == check['gate'])
                def process(ref):
                    data['process'] = ref
                    self.journal.save_validation(self.group, data)
                execution_plan = {**self.plan, 'gates': gates}
                evidence = verifier.run(execution_plan, target, {**self.definition, 'checks': [check]}, data['worktree'],
                    trigger=gate['trigger'], should_stop=self.stop_reason, on_process=process,
                    remaining=lambda: max(0, self.group['deadline'] - time.time()))[0]
                evidence['commit'] = binding['commit']
                from .reproducibility import atomic_json
                atomic_json(artifact, {'binding': binding, 'definition': check, 'evidence': evidence})
            data['evidence'] = [e for e in data['evidence'] if e['check_id'] != check['id']] + [evidence]
            data['criteria'] = self.criterion_status(checks, data['evidence'])
            self.journal.save_validation(self.group, data)
        self.check_running()
        pending = [e for e in data['evidence'] if e['status'] != 'PASS']
        data.update(state='failed' if pending else 'verified',
            criteria=self.criterion_status(checks, data['evidence']))
        self.journal.save_validation(self.group, data)
        if pending:
            return self.handle_failures(data, pending)
        return self.publish(data, checks)

    def handle_failures(self, validation, pending):
        self.group.update(state=self.scope + '_validation_failed', active_gate=None)
        self.controller.journal.save(self.group)
        issues = []
        for evidence in pending:
            # Identity deliberately excludes commit, log text, runtime and plan revision.
            identity = {'milestone': self.milestone['id'], 'gate': evidence['gate'], 'check': evidence['check_id']}
            if self.scope == 'project':
                identity['scope'] = 'project'  # Cannot collide with a milestone actually named "project".
            key = fingerprint(identity)
            prior = next((i for i in self.journal.issues() if i['id'] == key), None)
            classification = (evidence.get('classification', 'decision_pending') if evidence.get('reason') == 'human_review_pending' else
                'implementation_defect' if evidence['status'] == 'FAIL' and
                'FACTORY_IMPLEMENTATION_FAILURE_V1' in evidence.get('log', '').splitlines() else 'infrastructure')
            issue = prior or {'id': key, 'milestone': self.milestone['id'], 'attempts': 0, 'history': [], 'state': 'open'}
            if validation['id'] not in {h['validation'] for h in issue['history']}:
                issue['history'].append({'validation': validation['id'], 'evidence': evidence})
            issue.update(classification=classification, state='open')
            self.journal.save_issue(self.group, issue)
            issues.append(issue)
        blocked = [i for i in issues if i['classification'] != 'implementation_defect']
        if blocked:
            decision = any(i['classification'] in ('decision_pending', 'ambiguous_criterion', 'scope_decision') for i in blocked)
            architectural = any(i['classification'] == 'architecture_change' for i in blocked)
            raise FactoryError('waiting_decision' if decision else 'architecture_proposal' if architectural else 'validation_pending',
                'Closure requires an explicit review/decision' if decision else 'Closure environment/check did not execute successfully',
                details={'issues': [{'id': i['id'], 'classification': i['classification']} for i in blocked]})
        self.remediation_guard(validation, issues)
        # Recover issue -> execution linkage even if the process died just after queue.
        for i in issues:
            if i['attempts'] >= 1 and not i.get('execution_id'):
                with self.store._connection() as db:
                    row = db.execute('SELECT execution_id FROM execution_requests WHERE id=?',
                                     (i.get('request_id'),)).fetchone()
                if row:
                    i['execution_id'] = row[0]
                    self.journal.save_issue(self.group, i)
        exhausted = [i for i in issues if i['attempts'] >= 1 and i.get('execution_id') and
                     self.controller.executions.get(i['execution_id'])['state'] == 'checkpoint']
        if exhausted:
            raise FactoryError('remediation_exhausted', 'The one automatic remediation/revalidation cycle is exhausted',
                details={'issues': [i['id'] for i in exhausted],
                         'next_step': 'Inspect the failing criterion and propose a scoped correction or request an explicit decision; architecture changes require a revised baseline.'})
        self.check_running()
        accepted = [a for a in self.controller.executions.acceptances() if a.get('continuation_id') == self.group['id']]
        if len(accepted) >= self.group['limits']['max_slices']:
            raise FactoryError('budget_exhausted', 'Remediation is work and cannot bypass the aggregate slice/unit limit')
        with self.store._connection() as db:
            check_budget(db, self.group['id'], dispatch=True)
        # One execution may repair several concrete failures; each consumes its own
        # persistent problem budget, reserved before dispatch and reused after a crash.
        issue = issues[0]
        unit_id = 'remediation_' + issue['id'].split(':')[-1][:32]
        failed_ids = [e['check_id'] for e in pending]
        members = self.members()
        criterion_keys, acceptance, criterion_checks = [], [], {}
        for e in pending:
            obligations = [('criterion:' + str(i), criteria(self.milestone)[i]) for i in e['criteria']]
            if not obligations:
                gate = next(g for g in self.plan['gates'] if g['id'] == e['gate'])
                obligations = [(gate['id'] + ':' + str(i), gate['checks'][i]) for i in e['gate_checks']]
            criterion_checks[e['check_id']] = []
            for key, text in obligations:
                if key not in criterion_keys:
                    criterion_keys.append(key); acceptance.append(text)
                criterion_checks[e['check_id']].append(criterion_keys.index(key))
        selected = deepcopy(members[0])
        selected.update(id=unit_id, title='Repair integrated milestone failure',
            objective='Correct the recorded integrated verification failures within the approved milestone scope.',
            scope=list(dict.fromkeys(t for s in members for t in s['scope'])),
            out_of_scope=list(dict.fromkeys(t for s in members for t in s['out_of_scope'])),
            requirements=self.milestone['requirements'], dependencies=[s['id'] for s in members],
            components=sorted({c for s in members for c in s['components']}),
            boundaries=sorted({c for s in members for c in s['boundaries']}),
            acceptance_criteria=acceptance,
            maturity='execution_ready', architecture_impact='expected_within_baseline',
            verification_expectation='Run the affected closure checks and scoped regressions; then accept code and repeat integrated closure.')
        for i in issues:
            i.update(attempts=1, state='remediating', request_id='closure:' + unit_id)
            self.journal.save_issue(self.group, i)
        from .execution_contract import allowed
        writable = sorted({p for a in self.controller.executions.acceptances() if a['sources'] == self.group['sources'] and
                           a['slice_id'] in {s['id'] for s in members} for p in a.get('changed_paths', [])
                           if allowed(p, self.group['policy']['write_paths'])})
        if not writable:
            raise FactoryError('remediation_scope_unknown', 'No accepted milestone file can be identified for a bounded correction; scope decision required')
        remediation = {'scope': self.scope, 'issues': [i['id'] for i in issues], 'checks': failed_ids, 'validation_id': validation['id'],
                       'write_paths': writable, 'criteria_checks': criterion_checks}
        execution = Execution(self.service, self.store).queue(self.group['runtime_id'], 'closure:' + unit_id,
            selected=selected, continuation=self.group, remediation=remediation,
            failure={'kind': self.scope + '_integration_failure', 'commit': validation['binding']['commit'], 'checks': pending})
        for i in issues:
            i['execution_id'] = execution['id']; self.journal.save_issue(self.group, i)
        self.group.update(remediation=remediation, execution_id=execution['id'], active_slice=unit_id,
                          state='remediating', next_slice=None)
        self.controller.journal.save(self.group)
        return None

    def remediation_guard(self, validation, issues):
        """Project closure additionally reserves one global, durable cycle."""

    def check_publication(self, validation, checks):
        self.check_running()
        self.check_candidate(validation)
        if self.binding() != validation['binding']:
            raise FactoryError('stale_evidence', 'Candidate or pertinent sources changed before milestone publication')
        evidence = validation['evidence']
        if ({e['check_id'] for e in evidence} != {c['id'] for c in checks} or not evidence or
                any(e['status'] != 'PASS' or e['code_id'] != validation['code_id'] for e in evidence)):
            raise FactoryError('validation_pending', 'Every mandatory check must have current PASS evidence')

    def check_candidate(self, validation):
        if code_identity(validation['worktree']) != validation['code_id']:
            raise FactoryError('stale_evidence', 'Candidate changed before acceptance')

    def publish(self, validation, checks):
        self.check_publication(validation, checks)
        evidence = validation['evidence']
        accepted = [a for a in self.controller.executions.acceptances() if a['sources'] == self.group['sources']]
        done = {a['slice_id'] for a in accepted}
        if any(s['id'] not in done for s in self.plan['slices'] if s['milestone'] == self.milestone['id']):
            raise FactoryError('validation_pending', 'Milestone still has unaccepted slices')
        receipt = {'id': fingerprint({'milestone': self.milestone['id'], 'binding': validation['binding']}),
            'milestone': self.milestone['id'], 'sources': self.group['sources'],
            'commit': validation['binding']['commit'], 'code_id': validation['code_id'],
            'binding': validation['binding'], 'definition_id': self.group['policy']['definition_id'],
            'validation_id': validation['id'], 'evidence': evidence, 'criteria': validation['criteria'],
            'slices': [{'slice_id': a['slice_id'], 'execution_id': a['execution_id'], 'commit': a['commit']} for a in accepted
                       if a['slice_id'] in {s['id'] for s in self.plan['slices'] if s['milestone'] == self.milestone['id']}],
            'coverage': [c for c in self.plan['coverage'] if c['requirement'] in self.milestone['requirements']],
            'requirement_acceptances': [], 'accepted_at': time.time(), 'continuation_id': self.group['id']}
        prior = self.journal.closed(self.group['sources'])
        all_closed = {**prior, self.milestone['id']: receipt}
        for contract in self.definition.get('requirement_acceptance', []):
            if not set(contract['milestones']) <= all_closed.keys():
                continue
            # Full requirement oracles must be rerun on THIS integrated candidate;
            # earlier milestone tests only establish their historical contribution.
            if not set(contract['checks']) <= {e['check_id'] for e in evidence if e['status'] == 'PASS'}:
                continue
            receipt['requirement_acceptances'].append({**contract, 'receipt': receipt['id'], 'commit': receipt['commit']})
        receipt['summary'] = {'criteria_verified': len(receipt['criteria']), 'checks_passed': len(evidence),
                              'accepted_slices': len(receipt['slices']), 'pending_project_validation': True}
        return self.persist_receipt(validation, receipt,
            fingerprint({'sources': receipt['sources'], 'definition_id': receipt['definition_id']}))

    def persist_receipt(self, validation, receipt, source_key):
        # A Git ref lock and SQLite writer transaction fence both the code candidate
        # and authoritative sources while publishing the single durable close event.
        from .execution_workspace import lock_accepted_ref
        with lock_accepted_ref(self.store.project, receipt['commit']):
            with self.store._connection(write=True) as db:
                fence(db, self.group)
                check_budget(db, self.group['id'])
                if db.execute('SELECT 1 FROM decisions WHERE answer IS NULL').fetchone():
                    raise FactoryError('waiting_decision', 'A decision opened before closure')
                # Concurrent source writes cannot cross this transaction boundary.
                self.check_candidate(validation)
                if self.binding() != validation['binding']:
                    raise FactoryError('stale_evidence', 'Sources changed before closure transaction')
                old = db.execute('SELECT data FROM ' + self.scope + '_acceptances WHERE milestone=? AND source_key=?',
                                 (receipt['milestone'], source_key)).fetchone()
                if old:
                    return json.loads(old[0])
                db.execute('INSERT INTO ' + self.scope + '_acceptances VALUES (?,?,?,?)',
                           (receipt['id'], receipt['milestone'], source_key, canonical(receipt)))
                Runtime.event(db, 'project_verified' if self.scope == 'project' else 'milestone_closed',
                              {'milestone': receipt['milestone'], 'receipt': receipt['id'], 'commit': receipt['commit']})
                for issue in self.journal.issues(self.milestone['id']):
                    issue.update(state='resolved', receipt=receipt['id'])
                    db.execute('UPDATE ' + self.scope + '_issues SET data=? WHERE id=?', (canonical(issue), issue['id']))
        return receipt
