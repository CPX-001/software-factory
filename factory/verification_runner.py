"""Trusted entry point mounted read-only into a test namespace; no controller imports."""
import importlib
import importlib.util
import json
from pathlib import Path
import sys
import unittest


def main():
    check = json.loads(Path('/check.json').read_text())
    sys.path.insert(0, '/workspace')
    if check['kind'] == 'python_behavior':
        module, name = check['target'].split(':')
        subject = importlib.import_module(module)
        source = getattr(subject, '__file__', None)
        if not source or not Path(source).resolve().is_relative_to('/workspace'):
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
    target = Path('/workspace') / check['target']
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
    if not result.wasSuccessful():
        print('FACTORY_IMPLEMENTATION_FAILURE_V1')
        return 1
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
