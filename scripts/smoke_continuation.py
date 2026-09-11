"""Prepare a NEW disposable two-slice scenario; --run starts ONE bounded real MCP run.

Model code is never supplied by this driver. All starts, authorization and recovery
use the product service. Phase fixtures are synthetic and validated, not model work.
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
from factory.execution_store import ExecutionStore
from factory.workflow import Store
from scripts.execution_smoke_fixture import prepare, upgrade_prepared


async def smoke(prepared, run):
    parameters = StdioServerParameters(command=sys.executable,
        args=['-m', 'factory.mcp_server', '--home', prepared['registry_home']],
        cwd=str(Path(__file__).resolve().parent.parent))
    report = {**prepared, 'real_model_requested': run, 'phase_inputs': 'synthetic_validated',
              'client': 'Real Python MCP stdio client; not Codex App UI', 'solution_source': 'runtime model only'}
    async def call(client, name, **arguments):
        value = (await client.call_tool(name, {'project': prepared['project']['id'], **arguments})).structured_content
        if not value['ok']:
            raise RuntimeError(json.dumps(value['error']))
        return value['data']
    async with Client(parameters) as client:
        await call(client, 'factory_execution_policy', policy=prepared['policy'], verification=prepared['verification'])
        if run:
            from scripts.diagnose_execution import inspect_runtime
            report['quota_preflight'] = await asyncio.to_thread(inspect_runtime,
                prepared['policy']['model'], prepared['policy']['effort'], prepared['policy']['quota_reserve_percent'])
            if report['quota_preflight']['status'] != 'ready':
                report['model_started'] = False
                return report  # No run deadline/attempt is consumed while waiting for quota.
            started = time.monotonic()
            response = await call(client, 'factory_execute', request_id='two-slice-smoke-once')
            report.update(launch_seconds=time.monotonic() - started, continuation_id=response['requested_continuation_id'],
                          state_at_disconnect=response['continuation']['state'])
    report['client_disconnected_at'] = time.time()
    if not run:
        return report
    # Deterministic test observation; no model supervises the process or asks it to continue.
    await asyncio.sleep(2)
    async with Client(parameters) as client:
        deadline = time.monotonic() + prepared['policy']['continuation']['max_seconds'] + 10
        previous = None
        while True:
            status = await call(client, 'factory_status')
            group = status['continuation']
            stage = (group['active_slice'], group['state'])
            if stage != previous:
                print(json.dumps({'slice': stage[0], 'state': stage[1]}), flush=True)
                previous = stage
            if group['state'] not in ('queued', 'preflight', 'refining', 'implementing', 'repairing', 'applying', 'verifying', 'publishing',
                                      'milestone_ready', 'milestone_validating', 'milestone_validation_failed', 'remediating', 'preparing_milestone'):
                break
            if time.monotonic() > deadline:
                break
            await asyncio.sleep(2)
        report['status'] = status
        report['inspection'] = await call(client, 'factory_inspect', view='execution')
        report['refinement'] = await call(client, 'factory_inspect', view='refinement', slice_id='s2')
    # Read durable receipts for both slices for the evidence report; never mutate state.
    store = Store(prepared['project']['path'])
    journal = ExecutionStore(store)
    report['acceptances'] = [a for a in journal.acceptances() if a.get('continuation_id') == report['continuation_id']]
    report['executions'] = [{**journal.get(a['execution_id']), 'attempts': journal.attempts(a['execution_id'])}
                            for a in report['acceptances']]
    from factory.milestone_store import MilestoneStore
    report['milestone_receipts'] = MilestoneStore(store).receipts()
    report['completed_after_disconnect'] = group['state'] == 'project_ready_for_validation'
    report['finished_at'] = time.time()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--model')
    parser.add_argument('--effort')
    parser.add_argument('--prepared', type=Path, help='Reuse/upgrade the existing unexecuted pilot report')
    args = parser.parse_args()
    if args.prepared:
        prepared = upgrade_prepared(json.loads(args.prepared.read_text()))
        root = Path(prepared['root'])
    else:
        if not args.model or not args.effort:
            parser.error('Provide --prepared, or explicit --model and --effort')
        root = Path(tempfile.mkdtemp(prefix='factory-continuation-smoke-'))
        prepared = prepare(root, args.model, args.effort, continuation=True)
    print(json.dumps({'prepared': str(root), 'project': prepared['project'], 'real_model_requested': args.run}), flush=True)
    report = asyncio.run(smoke(prepared, args.run))
    target = root / 'report.json'; target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'report': str(target), 'state': report.get('status', {}).get('state', 'prepared')}), flush=True)
    return 0 if not args.run or report.get('completed_after_disconnect') else 1


if __name__ == '__main__':
    raise SystemExit(main())
