"""Worker-only Codex configuration: do not expose the Factory interface recursively."""
import json
import os
from pathlib import Path
import tomllib

from .workflow import WorkflowError


def worker_overrides():
    overrides = ['forced_login_method="chatgpt"', 'model_provider="openai"',
                 # Installed runtimes do not reliably honor plugin enabled overrides.
                 # A complete disabled transport overrides the bundled server. It is
                 # process-local and never executes this fallback command.
                 'mcp_servers.software_factory={command="python3",args=["-m","factory.mcp_server"],enabled=false}']
    path = Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')) / 'config.toml'
    try:
        config = tomllib.loads(path.read_text()) if path.is_file() else {}
    except (OSError, ValueError) as exc:
        raise WorkflowError('Cannot read worker Codex configuration') from exc
    for name in config.get('plugins', {}):
        if name.split('@', 1)[0] == 'software-factory':
            overrides.append(f'plugins.{json.dumps(name)}.enabled=false')
    for name, entry in config.get('mcp_servers', {}).items():
        args = entry.get('args', [])
        if 'factory.mcp_server' in args or any(str(arg).endswith('/scripts/factory_mcp.py') for arg in args):
            overrides.append(f'mcp_servers.{json.dumps(name)}.enabled=false')
    return tuple(overrides)
