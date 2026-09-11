"""Deterministic local projection of an immutable project receipt."""
import hashlib
from pathlib import Path
import shlex

from .registry import FactoryError
from .reproducibility import atomic_json, atomic_text, export_commit


def deliver(store, receipt):
    root = store.path.parent / 'deliveries' / receipt['id']
    root.mkdir(parents=True, exist_ok=True)
    exported = export_commit(store.project, receipt['commit'], root / 'source')
    if exported['code_id'] != receipt['code_id']:
        raise FactoryError('stale_evidence', 'Delivery export differs from the validated commit')
    runner = Path(__file__).with_name('verification_runner.py')
    expected = receipt['binding']['environment']['tools'][str(runner)]
    runner_copy = root / 'verification_runner.py'
    resource = store.path.parent / 'projects' / receipt['id'] / 'verification_runner.py'
    content = resource.read_bytes() if resource.is_file() else runner.read_bytes()
    if hashlib.sha256(content).hexdigest() != expected:
        if not runner_copy.is_file() or hashlib.sha256(runner_copy.read_bytes()).hexdigest() != expected:
            raise FactoryError('delivery_resource_unavailable', 'The version of the verification runner used by this receipt is unavailable')
    else:
        atomic_text(runner_copy, content.decode('utf-8'))
    atomic_json(root / 'receipt.json', receipt)
    atomic_json(root / 'contract.json', receipt['contract'])
    checks = {c['id']: c for c in receipt['contract']['verification']['checks']}
    lines = ['# Entrega local verificada', '', receipt['project']['name'], '',
             'Commit validado: `' + receipt['commit'] + '`.', '',
             'Código: `source/`. Recibo: `receipt.json`. Contrato y fuentes: `contract.json`.', '',
             '## Alcance aceptado', '']
    requirements = {r['key']: r['text'] for r in receipt['contract']['requirements']}
    lines += ['- ' + requirements[c['requirement']] for c in receipt['contract']['coverage'] if c['disposition'] == 'covered']
    lines += ['', '## Ejecución comprobada', '', 'Desde este directorio, con Python de sistema y biblioteca estándar:', '', '```bash']
    for key in receipt['contract']['verification']['project_acceptance']['entry_checks']:
        entry = checks[key].get('entrypoint')
        if entry:
            lines.append('(cd source && ' + shlex.join(['/usr/bin/python3', '-S', entry['path'], *entry['args']]) + ')')
        else:
            lines.append('# API comprobada: ' + checks[key]['target'])
            lines.append(shlex.join(['/usr/bin/python3', '-I', '-S', 'verification_runner.py',
                                    '--workspace', 'source', '--check', 'checks/' + key + '.json']))
    lines += ['```', '', '## Comprobaciones realizadas', '',
              'Se ejecutaron en una exportación limpia, de solo lectura, sin red y con entorno temporal vacío.',
              'Los comandos siguientes repiten los mismos oráculos; el sandbox original y sus hashes constan en el recibo.', '', '```bash']
    for evidence in receipt['evidence']:
        c = checks[evidence['check_id']]
        atomic_json(root / 'checks' / (c['id'] + '.json'), c)
        if c['kind'] != 'human_review':
            lines.append(shlex.join(['/usr/bin/python3', '-I', '-S', 'verification_runner.py',
                                    '--workspace', 'source', '--check', 'checks/' + c['id'] + '.json']))
    lines += ['```', '']
    lines += ['- ' + e['check_id'] + ': ' + e['status'] + ' (' + e.get('integration_mode', 'human_review') + ')' for e in receipt['evidence']]
    lines += ['', '## Límites y trabajo diferido', '']
    lines += ['- ' + x for x in receipt['limitations']]
    lines += ['- ' + e['requirement'] + ': ' + e['disposition'] + ' — ' + e['rationale'] +
              ' (decisión ' + str(e['authorization']['id']) + ')' for e in receipt['exclusions']]
    report = '\n'.join(lines) + '\n'
    atomic_text(root / 'REPORT.md', report)
    return str(root / 'REPORT.md')
