"""Build a private, portable installer ZIP using only the Python standard library."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from plugin_bundle import payload_files

ROOT = Path(__file__).resolve().parent.parent


def build(root, output):
    root, output = Path(root).resolve(), Path(output).resolve()
    files = {str(path): (root / path).read_bytes() for path in payload_files(root)}
    files['install.py'] = b"import runpy\nfrom pathlib import Path\nrunpy.run_path(str(Path(__file__).resolve().parent / 'scripts/install_codex_plugin.py'), run_name='__main__')\n"
    files['README.md'] = files['docs/private-plugin.md'].replace(b'](codex.md)', b'](docs/codex.md)')
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix('.zip.tmp')
    try:
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in sorted(files.items()):
                info = zipfile.ZipInfo('software-factory/' + name, date_time=(2026, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, content)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix + '.sha256').write_text(f'{digest}  {output.name}\n')
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    version = json.loads((ROOT / '.codex-plugin/plugin.json').read_text())['version'].split('+')[0]
    parser.add_argument('--output', type=Path, default=ROOT / f'dist/software-factory-{version}-private.zip')
    args = parser.parse_args()
    print(build(ROOT, args.output))


if __name__ == '__main__':
    main()
