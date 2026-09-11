"""Independent check for storage side effects on the declared CLI examples.

Uses Python audit events in the existing clean runner. This checks exercised
Python paths, not arbitrary native code or every possible input.
"""
import subprocess
import sys
import unittest


GUARDED_ENTRY = r'''
import os, runpy, sys
sys.dont_write_bytecode = True
sys.path.insert(0, os.getcwd())
violations = []
mutations = {'os.mkdir', 'os.remove', 'os.rmdir', 'os.rename', 'os.link',
             'os.symlink', 'os.truncate', 'os.chmod', 'os.chown', 'os.utime',
             'sqlite3.connect', 'os.system', 'os.exec', 'os.posix_spawn', 'subprocess.Popen'}
def audit(event, args):
    writing = event == 'open' and (
        isinstance(args[1], str) and any(c in args[1] for c in 'wax+') or
        isinstance(args[2], int) and args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
    if event in mutations or writing:
        violations.append(event)
        raise RuntimeError('PRODUCT_STORAGE_SIDE_EFFECT:' + event)
sys.addaudithook(audit)
sys.argv = ['category_report.py', sys.argv[1]]
code = 0
try:
    runpy.run_path('category_report.py', run_name='__main__')
except SystemExit as exc:
    code = exc.code
finally:
    if violations:
        sys.stderr.write('PRODUCT_STORAGE_SIDE_EFFECT:' + ','.join(violations) + '\n')
        code = 99
sys.exit(code)
'''


class NoResidualStorage(unittest.TestCase):
    def test_declared_cli_paths_do_not_use_storage(self):
        for name in ('valid', 'reordered', 'empty', 'negative', 'boolean', 'malformed'):
            with self.subTest(fixture=name):
                result = subprocess.run([sys.executable, '-I', '-S', '-B', '-c',
                    GUARDED_ENTRY, 'examples/' + name + '.json'],
                    capture_output=True, text=True, timeout=3)
                self.assertNotIn('PRODUCT_STORAGE_SIDE_EFFECT:', result.stderr)
                self.assertEqual(result.returncode, 2 if name in ('negative', 'boolean', 'malformed') else 0,
                                 result.stderr)
