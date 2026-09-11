"""Explicit, credential-free payload shared by private packaging and installation."""
from pathlib import Path


def payload_files(root):
    root = Path(root).resolve()
    paths = [root / name for name in (
        'requirements.txt', 'scripts/install_codex_plugin.py',
        'scripts/plugin_bundle.py', 'scripts/factory_mcp.py',
        'scripts/check_codex_plugin.py', 'scripts/plugin_mcp.py',
        'docs/private-plugin.md', 'docs/codex.md',
        'scripts/vendor/plugin_creator/NOTICE.md',
    )]
    paths += list((root / 'factory').rglob('*.py'))
    paths += list((root / 'scripts/vendor/plugin_creator').glob('*.py'))
    paths += [root / '.codex-plugin/plugin.json', root / '.mcp.json',
              root / 'plugin.json', root / 'mcp.json']
    paths += list((root / 'skills').rglob('*.md'))
    for path in sorted(set(paths)):
        relative = path.relative_to(root)
        if '__pycache__' in relative.parts:
            continue
        if path.resolve() != path or not path.is_file():
            raise ValueError(f'Payload file is missing or is a symlink: {relative}')
        yield relative
