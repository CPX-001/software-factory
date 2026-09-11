"""Check agreed oracle integrity and execute documented Python procedures offline.

The Factory clean-copy runner provides isolation and durable evidence. This file
does not create another checkout, sandbox, orchestrator or acceptance receipt.
"""
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import unittest


class DeliveryContract(unittest.TestCase):
    def test_original_oracles_unchanged(self):
        contract = json.loads(Path('pilot-contract.json').read_text())
        for name, expected in contract['resource_hashes'].items():
            self.assertEqual(hashlib.sha256(Path(name).read_bytes()).hexdigest(), expected, name)

    def test_documented_commands_really_execute(self):
        document = Path('USAGE.md').read_text()
        snippets = re.findall(r'```(?:bash|sh|shell|console|text)?\s*\n(.*?)```|`([^`\n]+)`', document, re.S)
        commands = []
        for block, inline in snippets:
            for line in (block or inline).splitlines():
                line = line.strip().removeprefix('$ ')
                if line.startswith(('python ', 'python3 ', '/usr/bin/python3 ')):
                    commands.append(shlex.split(line))
                elif block and line and not line.startswith('#'):
                    self.fail('A command block must contain only declared Python procedures: ' + line)
        self.assertTrue(commands, 'Document an executable Python usage command')
        entry_seen = False
        for command in commands:
            arguments = command[1:]
            if arguments[:1] == ['-S']:
                arguments = arguments[1:]
            entry = arguments == ['category_report.py', 'examples/valid.json']
            suites = ['test_records.py', 'test_category_report.py', 'test_product_cli.py']
            suite_command = (arguments[:2] == ['-m', 'unittest'] and
                bool([p for p in arguments[2:] if p != '-v']) and
                set(arguments[2:]) <= set(suites + [p[:-3] for p in suites] + ['-v']))
            self.assertTrue(entry or suite_command, 'Use the declared CLI or exact predeclared unittest files: ' + repr(command))
            result = subprocess.run([sys.executable, '-S', *arguments], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            if entry:
                entry_seen = True
                self.assertEqual(result.stdout, '[{"category":"food","count":2,"total":12},{"category":"books","count":1,"total":7}]\n')
                self.assertEqual(result.stderr, '')
            else:
                self.assertRegex(result.stderr, r'Ran [1-9][0-9]* tests?')
                self.assertIn('\nOK', result.stderr)
                self.assertNotIn('skipped=', result.stderr)
            print('VERIFIED_DOCUMENTED_COMMAND: ' + ' '.join(command))
        self.assertTrue(entry_seen, 'Document the real prescribed product entry')
