"""Opt-in real Codex smoke: a tiny product, all phases, no product implementation."""
import argparse
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from factory.application import FactoryService
from factory.registry import Registry

BRIEF = '''Quiero una utilidad CLI local-only offline personal para un usuario: contar palabras
sin contar manualmente. MVP: recibe una única cadena como argumento y escribe el número entero
de palabras a stdout; palabra es cada token separado por espacios Unicode. Una cadena vacía
produce 0. Éxito: "hola mundo" produce 2, "hola" produce 1 y "" produce 0. No incluye lectura
de archivos, red, persistencia, cuentas, UI gráfica, instalación distribuida ni historial.
No hay integraciones: ninguna. Los datos son texto no sensible suministrado por el propio
usuario; no hay autenticación ni envío de datos. Restricción: Python y biblioteca estándar,
sin dependencias de runtime externas ni gasto operativo. Escala: una invocación local cada vez,
como máximo 100 KB de texto, sin concurrencia. El usuario final es quien ejecuta la CLI para
obtener el recuento. El problema es evitar el recuento manual de un texto corto. Esto define
el alcance completo. Usa supuestos convencionales reversibles para detalles menores.'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--planning-only', action='store_true', help='Fixture discovery/architecture; real planning worker')
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix='factory-planning-smoke-'))
    registry = Registry(root / 'registry'); registry.allow_root(root)
    jobs = []
    service = FactoryService(registry, workflow_mode='verified', launcher=lambda p, r: jobs.append((p, r)))
    service.initialize_project(str(root / 'word-count'))
    print('Smoke artifacts: ' + str(root), flush=True)
    if args.planning_only:
        from factory.architecture import Architecture
        from factory.discovery import Discovery
        from factory.skill_catalog import Catalog
        from factory.skill_router import SkillRouter
        from factory.workflow import Store
        from tests.discovery_fakes import FakeModel, complete_reply
        from tests.architecture_fakes import FakeArchitect, proposal, review
        # Explicit synthetic specification: bounded Linux CLI with no external IO.
        fixture = complete_reply()
        texts = {'vision': 'Offline local-only personal word-count utility for a single user.',
            'users': 'Single user running a local Linux CLI.', 'problem': 'Avoid manual word counting.',
            'capabilities': 'Accept one string argument and print the count of Unicode whitespace-separated tokens.',
            'scope': 'A Python CLI accepts up to 1 KB of text on Linux and prints an integer count.',
            'success': 'An empty string produces 0, hello produces 1, hello world produces 2.',
            'out_of_scope': 'No file input, network, storage, GUI, or distribution packaging.',
            'constraints': 'Python standard library only on Linux, zero operating cost.',
            'scale': 'Single local invocation at a time, maximum 1 KB argument.',
            'security': 'Only non-sensitive user-supplied text, no network or accounts.',
            'integrations': 'none'}
        for item in fixture['knowledge']:
            item['text'] = texts[item['key']]
        store = Store(root / 'word-count')
        Discovery(store, FakeModel(fixture)).submit('Synthetic smoke specification')
        architecture = proposal(store.snapshot()['discovery']['knowledge'])
        Architecture(store, FakeArchitect(architecture, review()), SkillRouter(Catalog())).run()
        service.resume()
    else:
        service.submit_user_message(BRIEF)
    service.run_pending(*jobs[0])
    status = service.get_status()
    print(json.dumps(status, ensure_ascii=False, indent=2), flush=True)
    passed = status['phase'] == 'execution' and status['autonomous_run']['status'] == 'implementation_boundary'
    if passed:
        plan = service.get_planning()['plan']
        print(json.dumps({'milestones': len(plan['milestones']), 'slices': len(plan['slices']),
            'next_slice': service.get_planning(view='next_slice')['slice']['id'],
            'harness': len(plan['harness']), 'gates': len(plan['gates'])}, indent=2), flush=True)
        assert list((root / 'word-count').iterdir()) == [root / 'word-count/.factory']
    return 0 if passed else 2


if __name__ == '__main__':
    raise SystemExit(main())
