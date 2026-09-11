"""Install a private Factory bundle into user-owned storage, independent of this source."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from plugin_bundle import payload_files

ROOT = Path(__file__).resolve().parent.parent
NAME = 'software-factory'
SOURCE = {'source': 'local', 'path': './plugins/software-factory'}


def run(command, **kwargs):
    return subprocess.run([str(part) for part in command], check=True, **kwargs)


def helper(root, name, *args, capture=False):
    return run([sys.executable, root / 'scripts/vendor/plugin_creator' / name, *args],
               capture_output=capture, text=True)


def check_marketplace(root, marketplace, destination):
    """Validate identifiers and ownership before any installation changes."""
    entry = None
    name = 'personal'
    if marketplace.exists():
        name = helper(root, 'read_marketplace_name.py', '--marketplace-path', marketplace,
                      capture=True).stdout.strip()
        catalog = json.loads(marketplace.read_text())
        entries = catalog.get('plugins', [])
        if not isinstance(entries, list) or not all(isinstance(item, dict) for item in entries):
            raise ValueError('Personal marketplace plugins must be an array of objects')
        matches = [item for item in entries if item.get('name') == NAME]
        if len(matches) > 1:
            raise ValueError('Duplicate Software Factory entries in the personal marketplace')
        entry = matches[0] if matches else None
        if entry and entry.get('source') != SOURCE:
            raise ValueError('Software Factory is registered from a different source; leaving it unchanged')
    if destination.exists():
        manifest = destination / '.codex-plugin/plugin.json'
        if destination.is_symlink() or not entry or not manifest.is_file():
            raise ValueError(f'Refusing to replace an unregistered plugin directory: {destination}')
        if json.loads(manifest.read_text()).get('name') != NAME:
            raise ValueError('Installed plugin identifier does not match Software Factory')
    return name, entry


def prepare_runtime(root, storage):
    files = list(payload_files(root))
    digest = hashlib.sha256()
    base_python = str(Path(getattr(sys, '_base_executable', sys.executable)).resolve())
    digest.update(f'{sys.version}|{sys.platform}|{base_python}'.encode())
    for relative in files:
        digest.update(str(relative).encode() + b'\0' + (root / relative).read_bytes() + b'\0')
    fingerprint = digest.hexdigest()
    runtime = storage / 'runtimes' / fingerprint[:24]
    python = runtime / '.venv/bin/python'
    ready = runtime / '.ready'
    if ready.is_file() and ready.read_text() == fingerprint:
        run([python, '-c', 'import factory.mcp_server; import openai_codex'], cwd=runtime)
        return runtime, python
    # Failed setups have never been activated. Completed runtimes remain in place
    # so detached workers can finish against the version that launched them.
    if runtime.exists():
        if ready.exists():
            raise ValueError(f'Unexpected runtime fingerprint: {runtime}')
        shutil.rmtree(runtime)
    runtime.mkdir(parents=True, mode=0o700)
    try:
        for relative in files:
            target = runtime / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / relative, target)
        print('Preparando el entorno privado de Software Factory…', flush=True)
        run([base_python, '-m', 'venv', runtime / '.venv'])
        run([python, '-m', 'pip', 'install', '--disable-pip-version-check',
             '-r', runtime / 'requirements.txt'], stdout=sys.stderr)
        run([python, '-c', 'import factory.mcp_server; import openai_codex'], cwd=runtime)
        ready.write_text(fingerprint)
    except BaseException:
        shutil.rmtree(runtime)
        raise
    return runtime, python


def codex_command(python, explicit=None):
    if explicit:
        executable = shutil.which(explicit)
        if not executable:
            raise ValueError(f'Codex executable not found: {explicit}')
    else:
        # The pinned SDK ships Codex; no separate CLI or authoring skill needed.
        executable = run([python, '-c',
            'from codex_cli_bin import bundled_codex_path; print(bundled_codex_path())'],
            capture_output=True, text=True).stdout.strip()
    run([executable, 'plugin', 'add', '--help'], capture_output=True, text=True)
    return executable


def check_installed(destination):
    config = destination / '.mcp.json'
    entry = json.loads(config.read_text())['mcpServers']['software_factory']
    runtime = Path(entry['args'][0]).parent.parent
    run([entry['command'], runtime / 'scripts/check_codex_plugin.py', config],
        cwd=destination, timeout=45)


def install(root=ROOT, *, user_home=None, codex=None, allow_roots=()):
    if sys.version_info < (3, 11):
        raise ValueError('Software Factory requires Python 3.11 or newer')
    if sys.platform not in ('linux', 'darwin'):
        raise ValueError('Use Linux, macOS, or WSL. Native Windows is not supported by the Factory runtime yet.')
    if not shutil.which('git'):
        raise ValueError('Git is required by Factory. Install Git and retry.')
    import fcntl

    root = Path(root).resolve()
    user_home = Path(user_home or Path.home()).resolve()
    destination = user_home / 'plugins' / NAME
    marketplace = user_home / '.agents/plugins/marketplace.json'
    storage = user_home / '.local/share/software-factory'
    registry = Path(os.environ.get('FACTORY_HOME', user_home / '.local/state/software-factory')).expanduser().resolve()
    roots = [Path(path).expanduser().resolve(strict=True) for path in allow_roots]
    if not all(path.is_dir() for path in roots):
        raise ValueError('--allow-root requires an existing directory')
    storage.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (storage / 'install.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError('Another Software Factory installation is running') from exc
        name, entry = check_marketplace(root, marketplace, destination)
        runtime, python = prepare_runtime(root, storage)
        executable = codex_command(python, codex)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.factory-install-', dir=destination.parent) as temporary:
            stage = Path(temporary) / NAME
            stage.mkdir()
            shutil.copytree(runtime / '.codex-plugin', stage / '.codex-plugin')
            shutil.copytree(runtime / 'skills', stage / 'skills')
            config = {'mcpServers': {'software_factory': {
                'command': str(python),
                'args': [str(runtime / 'scripts/factory_mcp.py'), '--home', str(registry)],
            }}}
            (stage / '.mcp.json').write_text(json.dumps(config, indent=2) + '\n')
            helper(runtime, 'update_plugin_cachebuster.py', stage)
            run([python, runtime / 'scripts/check_codex_plugin.py', stage / '.mcp.json'],
                cwd=stage, timeout=45)
            previous_catalog = marketplace.read_bytes() if marketplace.exists() else None
            backup = Path(temporary) / 'previous-plugin'
            had_destination = destination.exists()
            if had_destination:
                destination.rename(backup)
            try:
                if not entry:
                    helper(runtime, 'create_basic_plugin.py', NAME, '--with-skills', '--with-mcp',
                           '--with-marketplace', '--path', destination.parent,
                           '--marketplace-path', marketplace)
                    shutil.rmtree(destination)  # Only the scaffold we just generated.
                stage.rename(destination)
                run([executable, 'plugin', 'add', f'{NAME}@{name}'])
            except BaseException:
                if destination.exists():
                    shutil.rmtree(destination)
                if had_destination:
                    backup.rename(destination)
                if previous_catalog is None:
                    marketplace.unlink(missing_ok=True)
                else:
                    marketplace.write_bytes(previous_catalog)
                raise
        for path in roots:
            run([python, '-c',
                 'import sys; from factory.registry import Registry; Registry(sys.argv[1]).allow_root(sys.argv[2])',
                 registry, path], cwd=runtime)
        print(f'Instalado: {NAME}@{name}\nMotor: {runtime}\nPlugin: {destination}')
        print('Puedes borrar el ZIP, su carpeta extraída y la copia de desarrollo. Abre un chat nuevo de Codex en este ordenador.')
        return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex', help='Optional Codex executable; defaults to the SDK-bundled CLI')
    parser.add_argument('--check', action='store_true', help='Check the installed MCP without model calls')
    parser.add_argument('--allow-root', action='append', default=[],
                        help='Optional allowed roots for historical verified projects')
    args = parser.parse_args()
    try:
        if args.check:
            check_installed(Path.home() / 'plugins' / NAME)
        else:
            install(codex=args.codex, allow_roots=args.allow_root)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.exit(1, f'No se pudo completar la instalación/comprobación: {exc}\n')


if __name__ == '__main__':
    main()
