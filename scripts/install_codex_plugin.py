"""Install this checkout as a personal local Codex plugin. No workflow execution."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from factory.application import FactoryService


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow-root', action='append', default=[], help='Optional allowed roots for historical verified projects')
    args = parser.parse_args()
    helpers = Path.home() / '.codex/skills/.system/plugin-creator/scripts'
    python = ROOT / '.venv/bin/python'
    if not python.is_file() or not helpers.is_dir():
        parser.error('This installer needs the repository .venv and the installed Codex plugin-creator skill')
    # Check dependencies before changing registration/configuration.
    subprocess.run([str(python), '-c', 'import mcp; import openai_codex; import yaml'], check=True)
    destination = Path.home() / 'plugins/software-factory'
    marketplace = Path.home() / '.agents/plugins/marketplace.json'
    source = ROOT / 'plugins/software-factory'
    if destination.exists():
        name = subprocess.check_output([sys.executable, str(helpers / 'read_marketplace_name.py')], text=True).strip()
        catalog = json.loads(marketplace.read_text())
        entry = next((p for p in catalog['plugins'] if p['name'] == 'software-factory'), None)
        if not entry or entry['source'] != {'source': 'local', 'path': './plugins/software-factory'}:
            parser.error('Existing software-factory plugin is not registered at this personal local destination')
        shutil.copytree(source, destination, dirs_exist_ok=True)
        subprocess.run([sys.executable, str(helpers / 'update_plugin_cachebuster.py'), str(destination)], check=True)
    else:
        subprocess.run([sys.executable, str(helpers / 'create_basic_plugin.py'), 'software-factory',
                        '--with-skills', '--with-mcp', '--with-marketplace'], check=True)
        shutil.copytree(source, destination, dirs_exist_ok=True)
        name = subprocess.check_output([sys.executable, str(helpers / 'read_marketplace_name.py')], text=True).strip()
    service = FactoryService()
    for root in args.allow_root:
        service.authorize_root(root)
    config = {'mcpServers': {'software_factory': {'command': str(python),
               'args': [str(ROOT / 'scripts/factory_mcp.py'), '--home', str(service.registry.home)]}}}
    (destination / '.mcp.json').write_text(json.dumps(config, indent=2) + '\n')
    subprocess.run([sys.executable, str(helpers / 'validate_plugin.py'), str(destination)], check=True)
    subprocess.run(['codex', 'plugin', 'add', 'software-factory@' + name], check=True)
    print('Installed software-factory. Open a new Codex App conversation on this host.')


if __name__ == '__main__':
    main()
