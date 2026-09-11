"""Native Git plugin entrypoint: provision the private runtime, then serve STDIO."""
import contextlib
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from install_codex_plugin import prepare_runtime


def main():
    if sys.version_info < (3, 11) or sys.platform not in ('linux', 'darwin'):
        raise SystemExit('Software Factory requires Python 3.11+ on Linux, macOS or WSL.')
    import fcntl

    root = Path(__file__).resolve().parent.parent
    storage = Path.home() / '.local/share/software-factory'
    storage.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Multiple Codex windows may start this MCP at once. Serialize provisioning
    # and keep ALL setup output off the MCP protocol's stdout.
    with (storage / 'install.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with contextlib.redirect_stdout(sys.stderr):
            runtime, python = prepare_runtime(root, storage)
    if sys.argv[1:] == ['--prepare']:
        print('Software Factory listo. Abre una conversación nueva de Codex.')
        return
    os.execv(str(python), [str(python), str(runtime / 'scripts/factory_mcp.py'), *sys.argv[1:]])


if __name__ == '__main__':
    main()
