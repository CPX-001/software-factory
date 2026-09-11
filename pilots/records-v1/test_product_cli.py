"""Independent pilot oracles. Copied unchanged before any product worker runs."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest


class ProductCLI(unittest.TestCase):
    def run_entry(self, name):
        return subprocess.run([sys.executable, '-S', 'category_report.py', 'examples/' + name],
                              capture_output=True, text=True, timeout=3)

    def test_valid_real_entry_and_exact_deterministic_output(self):
        expected = '[{"category":"food","count":2,"total":12},{"category":"books","count":1,"total":7}]\n'
        for _ in range(2):
            result = self.run_entry('valid.json')
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, expected, ''))

    def test_reordered_input_keeps_output(self):
        a, b = self.run_entry('valid.json'), self.run_entry('reordered.json')
        self.assertEqual(a.returncode, 0, a.stderr)
        self.assertEqual(b.returncode, 0, b.stderr)
        self.assertEqual(a.stdout, b.stdout)

    def test_invalid_file_has_no_partial_result(self):
        for name in ('negative.json', 'boolean.json', 'malformed.json'):
            with self.subTest(name=name):
                result = self.run_entry(name)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, '')
                self.assertTrue(result.stderr.strip())

    def test_empty_input(self):
        result = self.run_entry('empty.json')
        self.assertEqual((result.returncode, result.stdout), (0, '[]\n'), result.stderr)

    def test_integrated_ranking_preserves_caller_records(self):
        from category_report import rank_categories
        values = json.loads(Path('examples/valid.json').read_text())
        original = copy.deepcopy(values)
        self.assertEqual(rank_categories(values), [
            {'category': 'food', 'count': 2, 'total': 12},
            {'category': 'books', 'count': 1, 'total': 7}])
        self.assertEqual(values, original)
