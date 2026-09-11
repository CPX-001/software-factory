"""ONE bounded real smoke via actual MCP + detached FactoryService, in a NEW repo.

Without --run this only prepares product/phase inputs and acceptance tests (zero
inference). --run is explicit authorization for <=2 turns/180s/8000 observed tokens,
retaining the default reserve. No model SDK calls are made by this driver.
"""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp import Client
from mcp.client.stdio import StdioServerParameters
from factory.registry import Registry
from scripts.execution_smoke_fixture import prepare


async def smoke(prepared, *, run):
    parameters = StdioServerParameters(command=sys.executable,
        args=['-m', 'factory.mcp_server', '--home', prepared['registry_home']],
        cwd=str(Path(__file__).resolve().parent.parent))
    report = {**prepared, 'real_model_requested': run, 'phase_inputs': 'synthetic_validated',
              'client': 'Python MCP stdio client, not Codex App UI', 'solution_source': 'runtime model only'}
    async def call(client, name, **arguments):
        response = (await client.call_tool(name, {'project': prepared['project']['id'], **arguments})).structured_content
        if not response['ok']:
            raise RuntimeError(json.dumps(response['error']))
        return response['data']
    async with Client(parameters) as client:
        await call(client, 'factory_execution_policy', policy=prepared['policy'], verification=prepared['verification'])
        if run:
            started = time.monotonic()
            launched = await call(client, 'factory_execute', request_id='records-smoke-once')
            report['launch_seconds'] = time.monotonic() - started
            report['execution_id'] = launched['execution']['id']
            report['run_id'] = launched['execution']['run_id']
            report['state_at_disconnect'] = launched['execution']['state']
    report['client_disconnected_at'] = time.time()
    if not run:
        return report
    # Deterministic test driver, no LLM supervisor. The launch client/server have exited.
    await asyncio.sleep(3)
    async with Client(parameters) as client:
        deadline = time.monotonic() + prepared['policy']['max_seconds'] + 10
        while True:
            status = await call(client, 'factory_status')
            state = status['execution']['state']
            if state not in ('queued', 'preflight', 'implementing', 'repairing', 'applying', 'verifying', 'publishing'):
                break
            if time.monotonic() > deadline:
                # Observe only; the controller's own persistent budget bounds work.
                break
            await asyncio.sleep(3)
        inspection = await call(client, 'factory_inspect', view='execution')
        report['execution'] = {k: inspection.get(k) for k in ('id', 'run_id', 'state', 'reason', 'diagnostic',
            'slice_id', 'sources', 'definition_id', 'harness_revision', 'base_commit', 'branch', 'commit', 'worktree',
            'runtime_info', 'usage', 'quota_before', 'quota_after', 'quota_after_diagnostic', 'quota', 'isolation', 'verification', 'attempts', 'publication')}
        report['completed_after_disconnect'] = inspection['state'] == 'checkpoint'
        report['finished_at'] = time.time()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--model', required=True)
    parser.add_argument('--effort', required=True)
    parser.add_argument('--register', action='store_true', help='Add only this disposable product to the installed plugin registry')
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix='factory-records-smoke-'))
    prepared = prepare(root, args.model, args.effort)
    print(json.dumps({'prepared': str(root), 'project': prepared['project'], 'real_model_requested': args.run}), flush=True)
    report = asyncio.run(smoke(prepared, run=args.run))
    if args.register:
        report['app_registered_project'] = Registry().register(prepared['project']['path'], 'Disposable records smoke', trusted=True)
    path = root / 'report.json'; path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'report': str(path), 'state': report.get('execution', {}).get('state', 'prepared'),
                      'execution_id': report.get('execution_id')}, indent=2), flush=True)
    return 0 if not args.run or report.get('completed_after_disconnect') else 1


if __name__ == '__main__':
    raise SystemExit(main())
