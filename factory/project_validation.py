"""Final acceptance is a specialization of the integrated closure gate, not a run engine."""
from copy import deepcopy
import json
from pathlib import Path
import time

from .architecture import canonical, fingerprint
from .execution import current_sources
from .milestone import MilestoneGate, criteria
from .milestone_store import MilestoneStore, fence
from .project_store import ProjectStore
from .registry import FactoryError
from .reproducibility import atomic_text, environment_identity, export_commit


def contract_snapshot(snapshot, definition, definition_id):
    """Only references and verbatim approved conditions; never infer a new success test."""
    plan = snapshot['planning']['roadmap']['plan']
    return {'schema_version': 1, 'sources': current_sources(snapshot),
            'requirements': snapshot['planning']['roadmap']['source']['requirements'],
            'definition_id': definition_id, 'verification': definition,
            'milestones': plan['milestones'], 'coverage': plan['coverage'], 'gates': plan['gates'],
            'decisions': [d for d in snapshot['decisions'] if any(d['id'] == e['decision_id']
                for e in definition.get('project_acceptance', {}).get('exclusions', []))]}


class ProjectGate(MilestoneGate):
    scope = 'project'
    directory = 'projects'

    def __init__(self, controller, group, snapshot, definition):
        super().__init__(controller, group, snapshot, definition)
        self.journal = ProjectStore(self.store)
        self.original_plan = deepcopy(self.plan)
        self.contract = contract_snapshot(snapshot, definition, group['policy']['definition_id'])
        with self.store._connection() as db:
            frozen = db.execute("SELECT data FROM project_contracts WHERE json_extract(data, '$.definition_id')=? ORDER BY rowid LIMIT 1",
                                (group['policy']['definition_id'],)).fetchone()
        if frozen:
            self.contract = json.loads(frozen[0])
        self.contract_id = fingerprint(self.contract)
        self.milestone = {'id': 'project', 'requirements': [c['requirement'] for c in self.plan['coverage']],
                          'success_criteria': [], 'closure_conditions': [], 'subjective_criteria': [],
                          'verification_gates': []}

    def members(self):
        return self.original_plan['slices']

    def closed_receipt(self):
        binding = self.binding()
        return next((r for r in self.journal.receipts() if r['binding'] == binding), None)

    def binding(self):
        value = super().binding()
        try:
            environment = environment_identity()
        except OSError as exc:
            raise FactoryError('validation_pending', 'The declared clean verification environment is unavailable',
                               details={'classification': 'infrastructure', 'resource': str(exc)}) from exc
        return {**value, 'contract_id': self.contract_id, 'environment': environment,
                'authorization': {k: v for k, v in (self.group.get('final_authorization') or {}).items()
                                  if k not in ('automatic_remediation', 'remediation_authorized_at')},
                'milestones': [{'receipt': r['id'], 'commit': r['commit']} for r in
                    MilestoneStore(self.store).closed(self.group['sources']).values()]}

    def obligations(self):
        plan, definition = self.original_plan, self.definition
        project = definition.get('project_acceptance')
        errors = []
        if not self.group.get('final_authorization', {}).get('enabled'):
            errors.append('final_validation_not_authorized')
        if not project:
            errors.append('project_acceptance_contract_missing')
            project = {'entry_checks': [], 'delivery_paths': [], 'exclusions': []}
        closed = MilestoneStore(self.store).closed(self.group['sources'])
        errors += ['milestone_not_closed:' + m['id'] for m in plan['milestones'] if m['id'] not in closed]
        contracts = {c['requirement']: c for c in definition.get('requirement_acceptance', [])}
        entries = set(project['entry_checks'])
        selected_checks = entries | {c for a in contracts.values() for c in a['checks']}
        selected_gates = {g['id'] for g in plan['gates'] if g['kind'] != 'local'}
        selected_gates.update(c['gate'] for c in definition['checks'] if c['id'] in selected_checks)
        gates = [deepcopy(g) for g in plan['gates'] if g['id'] in selected_gates]
        checks = [deepcopy(c) for c in definition['checks'] if c['gate'] in selected_gates]
        by_id = {c['id']: c for c in checks}
        if not entries:
            errors.append('product_entry_acceptance_missing')
        for key in entries:
            if key not in by_id or (by_id[key]['kind'] != 'python_behavior' and not by_id[key].get('entrypoint')):
                errors.append('real_product_entry_missing:' + key)
        original_targets = {m['id']: (criteria(m), m.get('subjective_criteria', [])) for m in plan['milestones']}
        original_targets.update({s['id']: (s['acceptance_criteria'], []) for s in plan['slices']})
        labels, subjective, indices, references = [], [], {}, []
        milestone_ids = {m['id'] for m in plan['milestones']}
        for g in gates:
            values, human = original_targets[g['target']]
            needed = (range(len(values)) if g['target'] in milestone_ids else
                      sorted({i for c in checks if c['gate'] == g['id'] for i in c['criteria']}))
            for i in needed:
                key = (g['target'], i)
                if key not in indices:
                    indices[key] = len(labels); labels.append(values[i])
                    references.append({'target': g['target'], 'criterion': i})
                    if i in human:
                        subjective.append(indices[key])
            for c in checks:
                if c['gate'] == g['id']:
                    c['criteria'] = [indices[(g['target'], i)] for i in c['criteria']]
            g['target'] = 'project'
        decisions = {d['id']: d for d in self.contract['decisions']}
        exclusions = {e['requirement']: e for e in project['exclusions']}
        self.exclusions = []
        for coverage in plan['coverage']:
            key, disposition = coverage['requirement'], coverage['disposition']
            if disposition == 'covered':
                acceptance = contracts.get(key)
                if not acceptance:
                    errors.append('full_requirement_acceptance_missing:' + key)
                    continue
                contributors = {m['id'] for m in plan['milestones'] if key in m['requirements']}
                if not contributors <= set(acceptance['milestones']) or not set(acceptance['milestones']) <= closed.keys():
                    errors.append('requirement_contributions_pending:' + key)
                index = len(labels); labels.append(acceptance['condition'])
                references.append({'requirement': key})
                for check_id in acceptance['checks']:
                    if check_id not in by_id:
                        errors.append('requirement_check_missing:' + key + ':' + check_id)
                    else:
                        by_id[check_id]['criteria'].append(index)
            elif disposition in ('deferred', 'out_of_scope'):
                decision = decisions.get(exclusions.get(key, {}).get('decision_id'))
                if (not coverage['rationale'] or not decision or not decision['answered_at'] or
                        (decision['answer'] or '').strip().lower() != 'accept' or
                        key not in decision['question'] or disposition not in decision['question']):
                    errors.append('prior_exclusion_authorization_missing:' + key)
                else:
                    self.exclusions.append({**coverage, 'authorization': decision})
            else:
                errors.append('requirement_blocked:' + key)
        for g in gates:
            if g['kind'] == 'integration' and any('integration_mode' not in c for c in checks if c['gate'] == g['id']):
                errors.append('integration_mode_undeclared:' + g['id'])
            covered = {i for c in checks if c['gate'] == g['id'] for i in c['gate_checks']}
            if covered != set(range(len(g['checks']))):
                errors.append('verification_definition_missing:' + g['id'])
        covered = {i for c in checks for i in c['criteria']}
        if covered != set(range(len(labels))):
            errors.append('project_criteria_unmapped')
        human = {i for c in checks if c['kind'] == 'human_review' for i in c['criteria']}
        if not set(subjective) <= human:
            errors.append('subjective_criteria_require_human_review')
        self.milestone.update(success_criteria=labels, subjective_criteria=subjective,
                              verification_gates=[g['id'] for g in gates])
        self.criterion_references = references
        with self.store._connection(write=True) as db:
            fence(db, self.group)
            db.execute('INSERT OR IGNORE INTO project_contracts VALUES (?,?)', (self.contract_id, canonical(self.contract)))
        self.group.update(project_contract=self.contract_id, pending_criteria=errors)
        self.controller.journal.save(self.group)
        return gates, checks, errors

    def prepare_candidate(self, data, root):
        result = export_commit(self.store.project, data['binding']['commit'], root / 'source')
        if data.get('code_id') and data['code_id'] != result['code_id']:
            raise FactoryError('stale_evidence', 'Candidate export identity changed')
        data.update(worktree=result['path'], code_id=result['code_id'], reproducibility=result)
        # Pin the external verification resource before execution/publication, so report
        # recovery does not depend on the Factory installation remaining unchanged.
        resource = root / 'verification_runner.py'
        if not resource.exists():
            atomic_text(resource, Path(__file__).with_name('verification_runner.py').read_text())
        required = self.definition['project_acceptance']['delivery_paths']
        missing = [p for p in required if p not in result['files']]
        if missing:
            raise FactoryError('validation_pending', 'Required product delivery documents/files are missing',
                               details={'pending': missing, 'classification': 'delivery_gap'})

    def remediation_guard(self, validation, issues):
        if not self.group.get('final_authorization', {}).get('automatic_remediation'):
            raise FactoryError('final_remediation_not_authorized', 'Failure evidence saved; automatic final remediation requires separate authorization')
        # One reservation for the entire project, across runs, revisions and all failure names.
        with self.store._connection(write=True) as db:
            fence(db, self.group)
            old = db.execute('SELECT * FROM project_remediation WHERE id=1').fetchone()
            if old and (old['validation_id'] != validation['id'] or
                        set(json.loads(old['issue_ids'])) != {i['id'] for i in issues}):
                raise FactoryError('remediation_exhausted', 'The single global final remediation/revalidation cycle is exhausted')
            if not old:
                db.execute('INSERT INTO project_remediation VALUES (1,?,?)',
                           (validation['id'], canonical([i['id'] for i in issues])))

    def check_candidate(self, validation):
        export_commit(self.store.project, validation['binding']['commit'], validation['worktree'])

    def publish(self, validation, checks):
        self.check_publication(validation, checks)
        # Re-evaluate original commitments at the publication boundary, never mutate them.
        _, _, errors = self.obligations()
        if errors:
            raise FactoryError('validation_pending', 'Final acceptance obligations changed', details={'pending': errors})
        reproducibility = {**validation['reproducibility'], 'status': 'PASS',
                           'environment': validation['binding']['environment']}
        receipt = {'id': validation['id'], 'milestone': 'project', 'status': 'project_verified',
            'project': self.service._project(self.controller.project_id()),
            'sources': self.group['sources'], 'commit': validation['binding']['commit'],
            'code_id': validation['code_id'], 'binding': validation['binding'],
            'definition_id': self.group['policy']['definition_id'], 'contract_id': self.contract_id,
            'contract': self.contract, 'validation_id': validation['id'],
            'criteria': [{**c, 'source': self.criterion_references[c['index']]} for c in validation['criteria']],
            'evidence': validation['evidence'],
            'approvals': [e['decision'] for e in validation['evidence'] if e.get('decision')],
            'authorization': self.group['final_authorization'],
            'requirement_acceptances': self.definition.get('requirement_acceptance', []),
            'exclusions': self.exclusions, 'reproducibility': reproducibility,
            'limitations': ['Validated only for the recorded criteria, commit and offline Python environment; no deployment or security certification.',
                *['Simulated integration: ' + e['check_id'] for e in validation['evidence'] if e.get('integration_mode') == 'simulated']],
            'accepted_at': time.time(), 'continuation_id': self.group['id']}
        return self.persist_receipt(validation, receipt, validation['id'])

    def run(self):
        receipt = super().run()
        if receipt:
            from .project_delivery import deliver
            deliver(self.store, receipt)
        return receipt
