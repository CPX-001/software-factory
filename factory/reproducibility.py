"""Export committed regular blobs, then use the existing offline test sandbox."""
import hashlib
import os
from pathlib import Path
import shutil
import tempfile

from .architecture import fingerprint
from .execution_contract import relative_path
from .execution_workspace import git_bytes, files
from .registry import FactoryError


def atomic_json(path, value):
    from .architecture import canonical
    atomic_text(path, canonical(value) + '\n')


def atomic_text(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.pending-')
    try:
        with os.fdopen(fd, 'w') as handle:
            handle.write(value)
            handle.flush(); os.fsync(handle.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(tmp).unlink(missing_ok=True)


def export_commit(repo, commit, destination):
    destination = Path(destination)
    entries = git_bytes(repo, 'ls-tree', '-rz', commit).split(b'\0')
    inventory, content = {}, {}
    total = 0
    for entry in filter(None, entries):
        meta, name = entry.split(b'\t', 1)
        mode, kind, oid = meta.decode().split()
        name = relative_path(name.decode())
        if (mode not in ('100644', '100755') or kind != 'blob' or
                any(p in ('.venv', 'venv', '__pycache__') for p in Path(name).parts) or name.endswith(('.pyc', '.pyo'))):
            raise FactoryError('unreproducible_input', 'Candidate contains nonportable or undeclared runtime material: ' + name)
        size = int(git_bytes(repo, 'cat-file', '-s', oid))
        total += size
        if size > 10_000_000 or total > 100_000_000 or len(inventory) >= 10000:
            raise FactoryError('worktree_too_large', 'Clean export exceeds the existing product limits')
        blob = git_bytes(repo, 'cat-file', 'blob', oid)
        content[name] = blob
        inventory[name] = {'sha256': hashlib.sha256(blob).hexdigest(), 'executable': mode == '100755'}
    if destination.exists():
        # files() intentionally omits controller directories in implementation worktrees.
        # A clean export has no such directories: reject extra protected material too.
        unexpected = False
        expected_directories = {str(parent) for name in inventory for parent in Path(name).parents if str(parent) != '.'}
        for parent, directories, names in os.walk(destination, followlinks=False):
            for name in directories + names:
                path = Path(parent) / name
                relative = path.relative_to(destination).as_posix()
                try:
                    relative_path(relative)
                except FactoryError:
                    unexpected = True
                if path.is_symlink():
                    unexpected = True
                if name in directories and relative not in expected_directories:
                    unexpected = True
        if destination.is_symlink() or unexpected or files(destination) != inventory:
            raise FactoryError('stale_evidence', 'Clean export changed after preparation')
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix='.export-', dir=destination.parent))
        try:
            for name, blob in content.items():
                target = staging / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(blob)
                target.chmod(0o755 if inventory[name]['executable'] else 0o644)
            os.rename(staging, destination)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    return {'commit': commit, 'code_id': fingerprint(inventory), 'files': inventory,
            'method': 'git-regular-blobs-v1', 'path': str(destination), 'tracked_only': True}


def clean_resources():
    """Explicit offline Python runtime; never expose an entire native package directory."""
    import sysconfig
    library = Path('/usr/lib/python' + system_python_version())
    native = Path('/usr/lib') / (sysconfig.get_config_var('MULTIARCH') or 'unavailable')
    paths = [Path('/usr/bin/python3'), library]
    for pattern in ('ld-linux*.so.*', 'libc.so.*', 'libm.so.*', 'libseccomp.so.*', 'libz.so.*',
                    'libssl.so.*', 'libcrypto.so.*', 'libsqlite3.so.*', 'libbz2.so.*', 'liblzma.so.*',
                    'libffi.so.*', 'libexpat.so.*', 'libcrypt.so.*', 'libuuid.so.*', 'libreadline.so.*',
                    'libtinfo.so.*', 'libncursesw.so.*', 'libpanelw.so.*', 'libdb-*.so', 'libgdbm*.so.*',
                    'libpthread.so.*', 'librt.so.*', 'libdl.so.*'):
        paths.extend(sorted(p for p in native.glob(pattern) if p.is_file()))
    paths.extend(sorted(p for p in Path('/lib64').glob('ld-linux*.so.*') if p.is_file()))
    return paths


def environment_identity():
    import platform
    root = Path(__file__).parent
    trusted = ['verification_runner.py', 'verification.py', 'execution_sandbox.py',
               'sandbox_entry.py', 'sandbox_supervisor.py', 'reproducibility.py',
               'milestone.py', 'project_validation.py', 'execution_contract.py']
    resources = clean_resources()
    paths = [root / p for p in trusted] + [Path('/usr/bin/unshare')] + [p for p in resources if p.is_file()]
    # Python -S excludes site packages. Hash its actual standard library and extension
    # modules as well as the interpreter; upgrading them invalidates reusable evidence.
    version = system_python_version()
    library = Path('/usr/lib/python' + version)
    stdlib = {str(p.relative_to(library)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted(library.rglob('*')) if p.is_file()}
    return {'profile': 'linux-namespaces-seccomp-python-stdlib-v1', 'runtime': 'python_stdlib',
            'python_flags': ['-I', '-S'], 'network': False, 'installation': False,
            'environment': 'fresh allowlist; empty home and temporary filesystem',
            'platform': platform.platform(),
            'resource_paths': [str(p) for p in resources],
            'stdlib_sha256': fingerprint(stdlib), 'python_version': version,
            'tools': {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}}


def system_python_version():
    import subprocess
    return subprocess.check_output(['/usr/bin/python3', '-I', '-S', '-c',
        'import sys; print(str(sys.version_info.major)+"."+str(sys.version_info.minor))'],
        env={'PATH': '/usr/bin:/bin'}, text=True, timeout=5).strip()
