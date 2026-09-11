"""Observe real module composition without replacing a dependency with a mock."""
import sys
import unittest
from test_summary_integration import assert_stdlib


class ReportIntegration(unittest.TestCase):
    def test_summary_output_drives_the_ranking(self):
        from records import summarize
        from category_report import rank_categories
        returned = []
        def trace(frame, event, arg):
            if event == 'return' and frame.f_code == summarize.__code__:
                returned.append(arg)
        previous = sys.getprofile()
        try:
            sys.setprofile(trace)
            actual = rank_categories([{'category': ' B ', 'amount': 2}, {'category': 'a', 'amount': 2}])
        finally:
            sys.setprofile(previous)
        self.assertEqual(len(returned), 1, 'Ranking must consume the summary API')
        expected = [{'category': k, **v} for k, v in returned[0]['by_category'].items()]
        expected.sort(key=lambda row: (-row['total'], row['category']))
        self.assertEqual(actual, expected)

    def test_declared_stdlib_imports(self):
        assert_stdlib(self, ['record_rules.py', 'records.py', 'category_report.py'])
