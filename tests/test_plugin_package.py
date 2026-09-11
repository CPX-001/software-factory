import json
import contextlib
import io
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts.build_codex_plugin import build
from scripts import install_codex_plugin as installer
from scripts import plugin_mcp
from scripts.plugin_bundle import payload_files


ROOT = Path(__file__).resolve().parent.parent


class PackageFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='factory packaging ')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'downloaded source'
        for relative in payload_files(ROOT):
            target = self.source / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, target)


class PackageTests(PackageFixture):
    def test_native_package_has_portable_entrypoint_and_matching_identity(self):
        portable = json.loads((self.source / 'plugin.json').read_text())
        overlay = json.loads((self.source / '.codex-plugin/plugin.json').read_text())
        self.assertEqual((portable['name'], portable['version']), (overlay['name'], overlay['version']))
        self.assertTrue((self.source / 'skills/software-factory/SKILL.md').is_file())
        server = json.loads((self.source / 'mcp.json').read_text())['mcpServers']['software_factory']
        # The portable MCP parser rejects timeout fields accepted in config.toml.
        self.assertLessEqual(set(server), {'type', 'command', 'args', 'env', 'cwd'})
        path = Path(server['args'][0].replace('${PLUGIN_ROOT}', str(self.source)))
        self.assertTrue(path.is_file())

    def test_bootstrap_keeps_provisioning_output_off_mcp_stdout(self):
        out, err = io.StringIO(), io.StringIO()

        def prepare(*args):
            print('setup progress')
            return self.source, Path(sys.executable)

        with patch.object(plugin_mcp.Path, 'home', return_value=self.root / 'bootstrap user'), \
             patch.object(plugin_mcp, 'prepare_runtime', side_effect=prepare), \
             patch.object(plugin_mcp.os, 'execv') as launch, \
             patch.object(sys, 'argv', ['plugin_mcp.py', '--home', str(self.root / 'registry')]), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            plugin_mcp.main()
        self.assertEqual(out.getvalue(), '')
        self.assertIn('setup progress', err.getvalue())
        self.assertEqual(launch.call_args.args[1][-2:], ['--home', str(self.root / 'registry')])

    def test_zip_is_reproducible_and_contains_only_installation_payload(self):
        for name in ('.env', '.venv/secret', '.factory/state.sqlite3', '.git/config', 'tests/private.txt'):
            target = self.source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('PRIVATE_SENTINEL')
        first = build(self.source, self.root / 'first.zip')
        second = build(self.source, self.root / 'second.zip')
        self.assertEqual(first.read_bytes(), second.read_bytes())
        with zipfile.ZipFile(first) as archive:
            self.assertIn('software-factory/install.py', archive.namelist())
            self.assertIn('software-factory/factory/runner.py', archive.namelist())
            self.assertFalse(any(b'PRIVATE_SENTINEL' in archive.read(name) for name in archive.namelist()))
            archive.extractall(self.root / 'unpacked')
        # The entrypoint starts with plain Python, without importing Factory or its SDK.
        result = subprocess.run([sys.executable, self.root / 'unpacked/software-factory/install.py', '--help'],
                                cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_build_rejects_payload_symlinks(self):
        target = self.source / 'factory/__init__.py'
        target.unlink()
        target.symlink_to(self.root / 'private.txt')
        (self.root / 'private.txt').write_text('PRIVATE_SENTINEL')
        with self.assertRaises(ValueError):
            build(self.source, self.root / 'unsafe.zip')


class InstallationTests(PackageFixture):
    def setUp(self):
        super().setUp()
        self.user = self.root / 'user with spaces'
        self.destination = self.user / 'plugins/software-factory'
        self.marketplace = self.user / '.agents/plugins/marketplace.json'
        self.runtime = self.user / '.local/share/software-factory/runtimes/test-version'
        self.commands = []
        self.fail_registration = False
        self.real_run = installer.run

        def prepared(root, storage):
            if not self.runtime.exists():
                shutil.copytree(root, self.runtime)
            return self.runtime, Path(sys.executable)

        def command(args, **kwargs):
            if args[0] == 'test-codex':
                self.commands.append(args)
                if self.fail_registration:
                    raise subprocess.CalledProcessError(1, args)
                return subprocess.CompletedProcess(args, 0)
            return self.real_run(args, **kwargs)

        self.patches = [patch.object(installer, 'prepare_runtime', side_effect=prepared),
                        patch.object(installer, 'codex_command', return_value='test-codex'),
                        patch.object(installer, 'run', side_effect=command)]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def seed_catalog(self, factory=False, source=None):
        entries = [{'name': 'another-plugin', 'source': {'source': 'local', 'path': './plugins/another-plugin'}}]
        if factory:
            entries.append({'name': installer.NAME, 'source': source or installer.SOURCE,
                            'policy': {'installation': 'AVAILABLE', 'authentication': 'ON_USE'},
                            'category': 'Productivity'})
        catalog = {'name': 'my_private_catalog', 'interface': {'displayName': 'My plugins'}, 'plugins': entries}
        self.marketplace.parent.mkdir(parents=True, exist_ok=True)
        self.marketplace.write_text(json.dumps(catalog))
        return catalog

    def test_fresh_install_survives_deleted_download_and_preserves_catalog(self):
        previous = self.seed_catalog()
        installer.install(self.source, user_home=self.user)
        catalog = json.loads(self.marketplace.read_text())
        self.assertEqual(catalog['name'], previous['name'])
        self.assertEqual(catalog['interface'], previous['interface'])
        self.assertEqual(catalog['plugins'][0], previous['plugins'][0])
        self.assertEqual(len(catalog['plugins']), 2)
        self.assertEqual(self.commands[-1][-1], 'software-factory@my_private_catalog')
        self.assertNotIn(str(self.source), (self.destination / '.mcp.json').read_text())
        shutil.rmtree(self.source)
        installer.check_installed(self.destination)  # Actual STDIO tools, no model calls.

    def test_new_user_gets_personal_catalog(self):
        installer.install(self.source, user_home=self.user)
        catalog = json.loads(self.marketplace.read_text())
        self.assertEqual(catalog['name'], 'personal')
        self.assertEqual(self.commands[-1][-1], 'software-factory@personal')

    def test_upgrade_preserves_registration_and_restores_previous_plugin_on_failure(self):
        self.seed_catalog()
        installer.install(self.source, user_home=self.user)
        before_catalog = self.marketplace.read_bytes()
        before_manifest = (self.destination / '.codex-plugin/plugin.json').read_bytes()
        marker = self.destination / 'existing-installation'
        marker.write_text('preserve if update fails')
        self.fail_registration = True
        with self.assertRaises(subprocess.CalledProcessError):
            installer.install(self.source, user_home=self.user)
        self.assertEqual(marker.read_text(), 'preserve if update fails')
        self.assertEqual(self.marketplace.read_bytes(), before_catalog)
        self.assertEqual((self.destination / '.codex-plugin/plugin.json').read_bytes(), before_manifest)
        self.fail_registration = False
        installer.install(self.source, user_home=self.user)
        self.assertEqual(self.marketplace.read_bytes(), before_catalog)
        self.assertFalse(marker.exists())

    def test_failed_first_registration_removes_only_new_entry_and_plugin(self):
        self.seed_catalog()
        before = self.marketplace.read_bytes()
        self.fail_registration = True
        with self.assertRaises(subprocess.CalledProcessError):
            installer.install(self.source, user_home=self.user)
        self.assertEqual(self.marketplace.read_bytes(), before)
        self.assertFalse(self.destination.exists())

    def test_different_plugin_source_is_not_overwritten(self):
        self.seed_catalog(factory=True, source={'source': 'url', 'url': 'https://example.com/plugin.git'})
        before = self.marketplace.read_bytes()
        with self.assertRaises(ValueError):
            installer.install(self.source, user_home=self.user)
        installer.prepare_runtime.assert_not_called()
        self.assertEqual(self.marketplace.read_bytes(), before)

    def test_missing_dependencies_do_not_change_existing_installation(self):
        self.seed_catalog()
        before = self.marketplace.read_bytes()
        installer.prepare_runtime.side_effect = subprocess.CalledProcessError(1, ['pip'])
        with self.assertRaises(subprocess.CalledProcessError):
            installer.install(self.source, user_home=self.user)
        self.assertEqual(self.marketplace.read_bytes(), before)
        self.assertFalse(self.destination.exists())


if __name__ == '__main__':
    unittest.main()
