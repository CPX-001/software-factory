"""Trusted entry point mounted read-only into a test namespace; no controller imports."""
import importlib
import importlib.util
import json
from pathlib import Path
import sys
import unittest
import subprocess
import argparse
import os


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', default='/workspace')
    parser.add_argument('--check', default='/check.json')
    args = parser.parse_args()
    workspace = Path(args.workspace).resolve()
    check = json.loads(Path(args.check).read_text())
    os.chdir(workspace)
    sys.path.insert(0, str(workspace))
    if check['kind'] == 'python_behavior':
        module, name = check['target'].split(':')
        subject = importlib.import_module(module)
        source = getattr(subject, '__file__', None)
        if not source or not Path(source).resolve().is_relative_to(workspace):
            print('Behavior target must be a product module, not an arbitrary system callable')
            return 124
        function = getattr(subject, name)
        for case in check['cases']:
            args, expected = json.loads(case['args_json']), json.loads(case['expected_json'])
            actual = function(*args)
            if type(actual) != type(expected) or actual != expected:
                print(f'Behavior mismatch: args={args!r}, expected={expected!r}, actual={actual!r}')
                print('FACTORY_IMPLEMENTATION_FAILURE_V1')
                return 1
        print(f"Executed {len(check['cases'])} fixed behavior cases")
        return 0
    target = workspace / check['target']
    if not target.is_file():
        print('Required test file is unavailable')
        return 124
    spec = importlib.util.spec_from_file_location('factory_slice_test', target)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    suite = unittest.defaultTestLoader.loadTestsFromModule(module)
    if suite.countTestCases() < check['min_tests']:
        print('Unexpected zero/insufficient tests')
        return 124
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.testsRun < check['min_tests'] or result.skipped or result.expectedFailures:
        print('Required validation skipped, expected failure or insufficient tests')
        return 124
    if any('ModuleNotFoundError:' in error or 'ImportError:' in error for _, error in result.errors):
        print('Verification dependency unavailable in the declared runtime')
        return 124
    if not result.wasSuccessful():
        print('FACTORY_IMPLEMENTATION_FAILURE_V1')
        return 1
    if check.get('entrypoint'):
        entry = check['entrypoint']
        if not (workspace / entry['path']).is_file():
            print('Required product entry is unavailable')
            return 124
        process = subprocess.run([sys.executable, '-S', str(workspace / entry['path']), *entry['args']],
                                 cwd=workspace, capture_output=True, text=True)
        if process.returncode and any(x in process.stderr for x in ('ModuleNotFoundError:', 'ImportError:')):
            print('Product entry dependency unavailable in the declared runtime: ' + process.stderr)
            return 124
        if process.returncode != 0 or process.stdout != entry['stdout']:
            print('Product entry failed: ' + repr((process.returncode, process.stdout, process.stderr)))
            print('FACTORY_IMPLEMENTATION_FAILURE_V1')
            return 1
        print('FACTORY_PRODUCT_ENTRY_COMPLETED_V1')
    return 0


if __name__ == '__main__':
    try:
        code = main()
        if code == 0:
            print('FACTORY_CHECK_COMPLETED_V1', flush=True)
        sys.exit(code)
    except (ModuleNotFoundError, ImportError) as exc:
        print('Verification dependency unavailable: ' + str(exc))
        sys.exit(124)
