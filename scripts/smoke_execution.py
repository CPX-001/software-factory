"""Disposable execution demo. Default: simulated SDK and REAL isolated checks, no quota.

--prepare creates an authorized synthetic project without starting an implementation.
--register makes that disposable project visible to the existing Codex App plugin registry.
Never points execution at this Factory repository or at an existing user product.
"""
import argparse
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from factory.execution_store import ExecutionStore
from factory.registry import Registry
from tests.execution_fakes import FakeSDK, fixture, result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare', action='store_true', help='Prepare for a later Codex App execution; no model call')
    parser.add_argument('--register', action='store_true', help='Explicitly add ONLY this temporary product to the local plugin registry')
    parser.add_argument('--model', help='For --prepare: exact model offered by your local Codex runtime')
    parser.add_argument('--effort', help='For --prepare: exact effort offered for that model')
    args = parser.parse_args()
    if args.prepare and (not args.model or not args.effort):
        parser.error('--prepare requires --model and --effort; runtime validates them before implementation')
    root = Path(tempfile.mkdtemp(prefix='factory-execution-demo-'))
    sdk = FakeSDK(result(0, tests=True), result(3))
    service, store, jobs, _ = fixture(root, sdk, harness_during=True)
    journal = ExecutionStore(store)
    if args.prepare:
        policy = {k: v for k, v in journal.policy().items() if k not in ('repository', 'definition_id', 'authorized_at')}
        policy.update(model=args.model, effort=args.effort, max_attempts=1, max_seconds=90, max_tokens=8000,
                      quota_reserve_percent=35)
        definition = journal.definition(journal.policy()['definition_id'])['verification']
        service.configure_execution(policy, definition)
    else:
        service.execute_next_slice(request_id='disposable-demo')
        service.run_pending(*jobs[0])
        if journal.latest()['state'] != 'checkpoint':
            print(json.dumps(service.inspect(view='execution'), indent=2))
            return 1
    project = service._project()
    if args.register:
        project = Registry().register(str(store.project), 'Disposable execution demo', trusted=True)
    print(json.dumps({'project': project, 'registry_home': str(Registry().home if args.register else service.registry.home),
        'temporary_root': str(root), 'execution': journal.inspect(), 'synthetic_plan': True,
        'model_calls': 0, 'note': 'Prepared only; use factory_execute' if args.prepare else 'Simulated SDK: real FAIL -> repair -> real PASS'},
        ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
