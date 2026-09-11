"""Read-only, zero-inference check of the actual SDK + isolated App Server.

Never emits exception bodies, account identity, credentials or auth file content.
No threads/turns, login, credits, resets or alternate runtimes are started.
"""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from factory.codex_execution import CodexExecution
from factory.execution_contract import DEFAULT_POLICY
from factory.execution_sandbox import LinuxSandbox
from factory.quota import diagnose, quota_guard


def binary_info(path):
    path = Path(path).resolve()
    version = subprocess.run([str(path), '--version'], capture_output=True, text=True, timeout=10)
    # --version is a local trusted runtime command, never a model or auth operation.
    return {'path': str(path), 'version': version.stdout.strip()[:100],
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def inspect_runtime(model, effort, reserve=DEFAULT_POLICY['quota_reserve_percent']):
    from openai_codex import CodexConfig
    from openai_codex.client import _resolve_codex_bin
    policy = {**DEFAULT_POLICY, 'model': model, 'effort': effort, 'quota_reserve_percent': reserve}
    report = {'observed_at': time.time(), 'sdk_version': importlib.metadata.version('openai-codex'),
              'bundled_runtime': binary_info(_resolve_codex_bin(CodexConfig())),
              'terminal_runtime': binary_info(shutil.which('codex')) if shutil.which('codex') else None,
              'method': 'account/rateLimits/read', 'model_calls': 0,
              'environment_presence': {k: bool(os.environ.get(k)) for k in
                  ('CODEX_HOME', 'HTTPS_PROXY', 'HTTP_PROXY', 'ALL_PROXY', 'SSL_CERT_FILE', 'SSL_CERT_DIR',
                   'OPENAI_API_KEY', 'CODEX_API_KEY')}, 'reserve_percent': reserve}
    with tempfile.TemporaryDirectory(prefix='factory-quota-diagnostic-') as tmp:
        directory = Path(tmp)
        product = directory / 'empty-product'; product.mkdir()
        sandbox = LinuxSandbox(directory / 'sandbox'); sandbox.lifetime = 30
        worker = None
        try:
            report['isolation'] = sandbox.probe()
            worker = CodexExecution(sandbox, policy, product)
            report['runtime'] = worker.runtime_info
            report['quota'] = worker.quota()
            quota_guard(report['quota'], policy)
            report['status'] = 'ready'
        except Exception as exc:
            cause = diagnose(exc)
            report.update(status='blocked', diagnostic={'code': cause.code, 'message': str(cause), **cause.details})
        finally:
            if worker:
                worker.close()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', required=True)
    parser.add_argument('--effort', required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    report = inspect_runtime(args.model, args.effort)
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        args.output.write_text(encoded)
    print(encoded)
    return 0 if report['status'] == 'ready' else 1


if __name__ == '__main__':
    raise SystemExit(main())
