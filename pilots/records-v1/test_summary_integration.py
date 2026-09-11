"""Supplement the unchanged initial oracles with the accepted integration criteria."""
import ast
from pathlib import Path
import sys
import unittest


def assert_stdlib(test, paths):
    for path in paths:
        for node in ast.walk(ast.parse(Path(path).read_text())):
            names = ([node.module.split('.')[0]] if isinstance(node, ast.ImportFrom) and node.module
                     else [a.name.split('.')[0] for a in node.names] if isinstance(node, ast.Import) else [])
            for name in names:
                test.assertIn(name, sys.stdlib_module_names | {'record_rules', 'records', 'category_report'})


class SummaryIntegration(unittest.TestCase):
    def test_existing_normalization_is_actually_called(self):
        import record_rules
        from records import summarize
        calls = []
        def trace(frame, event, arg):
            if event == 'call' and frame.f_code == record_rules.normalized_category.__code__:
                calls.append(frame.f_locals.copy())
        previous = sys.getprofile()
        try:
            sys.setprofile(trace)
            result = summarize([{'category': ' STRASSE ', 'amount': 2}, {'category': 'Straße', 'amount': 0}])
        finally:
            sys.setprofile(previous)
        self.assertEqual(result, {'count': 2, 'total': 2, 'by_category': {'strasse': {'count': 2, 'total': 2}}})
        self.assertEqual(len(calls), 2, 'Use the established helper for each category')

    def test_declared_stdlib_imports(self):
        assert_stdlib(self, ['record_rules.py', 'records.py'])
