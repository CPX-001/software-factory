"""Check the independent oracle against disposable products, without inference."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class StorageOracleTests(unittest.TestCase):
    def test_oracle_accepts_stateless_cli_and_rejects_caught_storage_side_effects(self):
        oracle = Path(__file__).resolve().parents[1] / 'pilots/records-v1/test_no_residual_storage.py'
        tail = "\nimport sys\nsys.exit(2 if sys.argv[1].split('/')[-1] in ('negative.json','boolean.json','malformed.json') else 0)\n"
        for action in ('', "open('residual.db','w').close()", "import tempfile; tempfile.TemporaryFile()",
                       "import sqlite3; sqlite3.connect(':memory:')"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'test_storage.py').write_bytes(oracle.read_bytes())
                product = ('try:\n    ' + action + '\nexcept Exception:\n    pass\n') if action else ''
                (root / 'category_report.py').write_text(product + tail)
                result = subprocess.run([sys.executable,'-I','-S','-B','-m','unittest','discover','-s',directory,
                                         '-p','test_storage.py'],cwd=root,capture_output=True,text=True,timeout=20)
                self.assertEqual(result.returncode, 1 if action else 0, result.stderr)
                self.assertEqual('PRODUCT_STORAGE_SIDE_EFFECT:' in result.stderr, bool(action))
                self.assertFalse((root / 'residual.db').exists())
