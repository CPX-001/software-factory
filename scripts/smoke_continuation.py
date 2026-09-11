"""Reuse the pending two-milestone scenario through final validation and local delivery.

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


def load_discovery_pilot(path):
    """Resume is a read of the same instance, never fixture recreation or an upgrade."""
    import hashlib
    from factory.registry import FactoryError, Registry
    value = json.loads(Path(path).read_text())
    if value.get('phase_inputs') != 'discovery_brief':
        raise FactoryError('pilot_kind_mismatch', 'This is not a from-discovery pilot')
    root, product = Path(value['root']), Path(value['project']['path'])
    if product != root / 'product' or not (product / '.factory/state.sqlite3').is_file():
        raise FactoryError('pilot_unavailable', 'The original pilot state is missing; do not recreate it as a resume')
    if not (Path(value['registry_home']) / 'registry.sqlite3').is_file():
        raise FactoryError('pilot_registry_unavailable', 'The original registry is unavailable on this host')
    project = Registry(value['registry_home']).resolve(value['project']['id'])
    if project != value['project']:
        raise FactoryError('pilot_identity_mismatch', 'The saved project does not match its registered identity')
    contract = product / 'pilot-contract.json'
    if hashlib.sha256(contract.read_bytes()).hexdigest() != value['contract_sha256']:
        raise FactoryError('pilot_contract_changed', 'The predeclared acceptance contract changed; review it explicitly')
    return value


def installed_parameters(prepared):
    """Use the actual local plugin launcher, not a second server configuration."""
    config = Path.home() / 'plugins/software-factory/.mcp.json'
    server = json.loads(config.read_text())['mcpServers']['software_factory']
    args = server['args']
    if '--home' not in args or args[args.index('--home') + 1] != prepared['registry_home']:
        raise ValueError('The installed plugin points at a different Factory registry')
    return StdioServerParameters(command=server['command'], args=args)


def independent_result(prepared, final):
    """Project checks already ran the predeclared oracles in Factory's clean runner."""
    receipts = final.get('receipts', [])
    if not receipts:
        return {'status': 'NOT_RUN', 'reason': 'No durable accepted-candidate receipt exists'}
    receipt = receipts[-1]
    definition = receipt['contract']['verification']
    checks = {c['id']: c for c in definition['checks']}
    matched = []
    for original in prepared['contract']['checks']:
        candidates = [e for e in receipt['evidence'] if e['status'] == 'PASS' and
            e['code_id'] == receipt['code_id'] and e.get('commit') == receipt['commit'] and
            all(checks[e['check_id']].get(k) == v for k, v in original.items()
                if k not in ('id', 'gate', 'criteria', 'gate_checks')) and
            receipt['reproducibility']['files'][original['target']]['sha256'] ==
                prepared['contract']['resource_hashes'][original['target']]]
        if not candidates:
            return {'status': 'NOT_RUN', 'reason': 'No matching final independent evidence: ' + original['id']}
        matched.append({'oracle': original['id'], 'target': original['target'], 'min_tests': original['min_tests'],
                        'evidence': candidates[0]['check_id'], 'status': 'PASS'})
    return {'status': 'PASS' if receipt['reproducibility']['status'] == 'PASS' else 'NOT_RUN',
            'commit': receipt['commit'], 'receipt': receipt['id'], 'checks': matched,
            'method': 'Original independent oracles executed by the existing clean project runner'}


async def discovery_smoke(prepared, run, *, installed=False, parameters=None):
    """Prepare/inspect through the normal MCP surface without faking phase progress.

    An explicit pre-planning policy enables the existing detached controller and its
    shared ledger. This driver does not authorize new budgets or supervise phases.
    """
    if parameters is None:
        parameters = installed_parameters(prepared) if installed else StdioServerParameters(command=sys.executable,
            args=['-m', 'factory.mcp_server', '--home', prepared['registry_home']],
            cwd=str(Path(__file__).resolve().parent.parent))
    report = {**prepared, 'real_model_requested': run, 'model_started': False,
        'client': 'Real MCP stdio client; installed plugin launcher' if installed else 'Real MCP stdio client; repository launcher',
        'codex_app_ui': 'not performed', 'model_calls_this_invocation': 0,
        'implementation_status': 'prepared; end-to-end acceptance incomplete',
        'integration_gaps': [
            {'code': 'workflow_budget_unavailable', 'source': 'factory/continuation_store.py:budget',
             'detail': 'Aggregate call/token/deadline enforcement begins with execution; discovery, architecture and planning are not covered.'},
            {'code': 'initial_execution_authorization_boundary', 'source': 'factory/controller.py:stop_reason',
             'detail': 'After real planning, typed verification and execution authorization must still be bound through the normal service; no initial whole-workflow authorization exists.'},
            {'code': 'analysis_effort_not_pinned', 'source': 'factory/codex_discovery.py; factory/codex_architecture.py; factory/codex_planning.py',
             'detail': 'The analysis adapters select the existing model but do not apply the execution effort policy.'}],
        'effective_models': {}, 'usage_by_phase': {}, 'human_answers': [],
        'interventions': [{'kind': 'initial_authorization', 'action': 'Prepare one fresh persistent pilot; retain the existing model, effort, reserve and finite limits.'},
                          {'kind': 'environment_or_quota', 'action': 'Keep the project paused before any model inference.'}]}
    async def call(client, name, **arguments):
        value = (await client.call_tool(name, arguments)).structured_content
        if not value['ok']:
            raise RuntimeError(json.dumps(value['error']))
        return value['data']
    identifier = prepared['project']['id']
    async with Client(parameters) as client:
        listed = await client.list_tools()
        report['tool_names'] = sorted(t.name for t in listed.tools)
        await call(client, 'factory_project', action='select', project=identifier)
        status = await call(client, 'factory_status', project=identifier)
        # Enqueue the declared test input once while paused; no worker or fake answer.
        if status['phase'] == 'discovery' and status['autonomous_run']['paused']:
            status = await call(client, 'factory_message', project=identifier,
                                message=prepared['initial_message'], request_id=prepared['request_id'])
        report['status_before_disconnect'] = status
    report['client_disconnected_at'] = time.time()
    async with Client(parameters) as client:
        status = await call(client, 'factory_status')
        if status['project']['id'] != identifier:
            raise RuntimeError('Selected project did not survive reconnect')
        explicit = await call(client, 'factory_status', project=identifier)
        if status['autonomous_run']['run_id'] != explicit['autonomous_run']['run_id']:
            raise RuntimeError('Reconnect changed the run identity')
        report['status'] = explicit
        report['selection_survived_reconnect'] = True
        report['run_id'] = explicit['autonomous_run']['run_id']
        report['continuation_id'] = (explicit.get('continuation') or {}).get('id')
        report['pending_decisions'] = await call(client, 'factory_decisions', project=identifier)
    effective_policy = ExecutionStore(Store(prepared['project']['path'])).policy()
    guarded = effective_policy.get('analysis_authorized') or bool((explicit.get('continuation') or {}).get('budget', {}).get('analysis_calls'))
    if guarded:
        report['policy'] = effective_policy
        report['full_workflow_limits'] = effective_policy['continuation']
        report['full_workflow_limits_enforced'] = True
        report['integration_gaps'] = [g for g in report['integration_gaps']
                                    if g['code'] == 'initial_execution_authorization_boundary' and effective_policy.get('analysis_authorized')]
    if run:
        from scripts.diagnose_execution import inspect_runtime
        report['quota_preflight'] = await asyncio.to_thread(inspect_runtime,
            report['policy']['model'], report['policy']['effort'], report['policy']['quota_reserve_percent'])
        if report['quota_preflight']['status'] != 'ready':
            report['blocker'] = report['quota_preflight']['diagnostic']
        elif guarded:
            async with Client(parameters) as client:
                explicit = await call(client, 'factory_resume', project=identifier)
                report['run_id'] = explicit['autonomous_run']['run_id']
                report['dispatch_requested'] = True
            # A disconnected client never owns the work or its spending controls.
            await asyncio.sleep(2)
            async with Client(parameters) as client:
                explicit = await call(client, 'factory_status', project=identifier)
            report['status'] = explicit
            report['continuation_id'] = (explicit.get('continuation') or {}).get('id')
            report['blocker'] = (explicit.get('continuation') or {}).get('diagnostic')
        else:
            report['blocker'] = report['integration_gaps'][0]
    else:
        report['blocker'] = ((explicit.get('continuation') or {}).get('diagnostic') if guarded
                             else report['integration_gaps'][0])
    if guarded and not report.get('blocker'):
        report['blocker'] = explicit.get('execution', {}).get('diagnostic')
    store = Store(prepared['project']['path'])
    snapshot = store.snapshot()
    report['phases_really_completed'] = [phase for phase, complete in (
        ('discovery', snapshot['discovery']['completed_at']),
        ('architecture', snapshot['architecture']['baseline']),
        ('planning', snapshot['planning']['roadmap'])) if complete]
    with store._connection() as db:
        completed_turns = db.execute("SELECT count(*) FROM discovery_turns WHERE status='completed'").fetchone()[0]
        calls = [dict(r) for r in db.execute('SELECT kind,data FROM continuation_calls')]
    report['usage_by_phase'] = {'discovery': {'completed_turns': completed_turns, 'token_usage': 'not recorded'},
        'architecture': {'calls': snapshot['architecture']['calls'], 'token_usage': 'not recorded'},
        'planning': {'calls': snapshot['planning']['calls'], 'token_usage': 'not recorded'},
        'execution_and_refinement': (explicit.get('continuation') or {}).get('budget')}
    if guarded:
        report['model_started'] = bool(calls)
        report['model_calls_this_invocation'] = None  # Observation is not a per-call trace.
        report['effective_models'] = {k: effective_policy[k] for k in ('model', 'effort')}
        for phase in ('discovery', 'architecture', 'planning'):
            phase_calls = [json.loads(c['data']) for c in calls if c['kind'] == phase]
            report['usage_by_phase'][phase] = {'calls': len(phase_calls),
                'tokens': sum((c.get('usage') or {}).get('total', {}).get('totalTokens', 0) for c in phase_calls),
                'unknown_usage_calls': sum(not c.get('usage') for c in phase_calls)}
    from factory.project_store import ProjectStore
    final = ProjectStore(store).inspect(full=True)
    report['independent_acceptance'] = independent_result(prepared, final)
    report['final_commit'] = (final['validated_version'] or {}).get('commit')
    report['final_receipt'] = (final['validated_version'] or {}).get('receipt')
    report['delivery'] = final['delivery']
    report['completed_after_disconnect'] = (final['state'] == 'project_verified' and final.get('delivery_ready') and
                                           report['independent_acceptance']['status'] == 'PASS')
    report['implementation_status'] = final['state']
    if final['validated_version']:
        report['phases_really_completed'].extend(['execution', 'milestone_closure', 'project_validation'])
    report['finished_at'] = time.time()
    return report


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
                                      'milestone_ready', 'milestone_validating', 'milestone_validation_failed', 'remediating', 'preparing_milestone',
                                      'project_ready', 'project_validating', 'project_validation_failed'):
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
    from factory.project_store import ProjectStore
    report['project_validation'] = ProjectStore(store).inspect(full=True)
    report['completed_after_disconnect'] = group['state'] == 'project_verified'
    report['finished_at'] = time.time()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--model')
    parser.add_argument('--effort')
    parser.add_argument('--prepared', type=Path, help='Reuse/upgrade the existing unexecuted pilot report')
    parser.add_argument('--from-discovery', action='store_true', help='Create the same small product with only its initial brief and independent oracles')
    parser.add_argument('--directory', type=Path, help='New persistent pilot directory outside Factory; never overwritten')
    parser.add_argument('--installed', action='store_true', help='Use the locally installed plugin launcher for the from-discovery pilot')
    args = parser.parse_args()
    if args.prepared:
        if args.from_discovery or args.directory or args.model or args.effort:
            parser.error('Resume with --prepared alone; creation and authorization changes are separate operations')
        value = json.loads(args.prepared.read_text())
        prepared = (load_discovery_pilot(args.prepared) if value.get('phase_inputs') == 'discovery_brief'
                    else upgrade_prepared(value))
        root = Path(prepared['root'])
    elif args.from_discovery:
        if not args.directory or not args.model or not args.effort:
            parser.error('From-discovery creation requires --directory, --model and --effort explicitly')
        from scripts.execution_smoke_fixture import prepare_from_discovery
        prepared = prepare_from_discovery(args.directory, args.model, args.effort)
        root = Path(prepared['root'])
    else:
        if args.directory or args.installed:
            parser.error('--directory/--installed belong to the from-discovery pilot')
        if not args.model or not args.effort:
            parser.error('Provide --prepared, or explicit --model and --effort')
        root = Path(tempfile.mkdtemp(prefix='factory-continuation-smoke-'))
        prepared = prepare(root, args.model, args.effort, continuation=True)
    print(json.dumps({'prepared': str(root), 'project': prepared['project'], 'real_model_requested': args.run}), flush=True)
    report = asyncio.run(discovery_smoke(prepared, args.run, installed=args.installed)
                         if prepared.get('phase_inputs') == 'discovery_brief' else smoke(prepared, args.run))
    from factory.reproducibility import atomic_json
    if prepared.get('phase_inputs') == 'discovery_brief':
        atomic_json(root / 'observations' / (str(time.time_ns()) + '.json'), report)
    target = root / 'report.json'; atomic_json(target, report)
    print(json.dumps({'report': str(target), 'state': report.get('status', {}).get('state', 'prepared'),
                      'blocker': report.get('blocker')}), flush=True)
    return 0 if not args.run or report.get('completed_after_disconnect') else 1


if __name__ == '__main__':
    raise SystemExit(main())
