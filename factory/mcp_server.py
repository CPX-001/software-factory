"""STDIO MCP transport. All behavior is delegated to FactoryService."""
import argparse
import asyncio
import json
import sqlite3

from .application import FactoryService
from .registry import FactoryError, Registry
from .workflow import WorkflowError

INSTRUCTIONS = '''Software Factory owns workflow and persistent state. Pin the registered project ID; server cwd is not the conversation workspace. Relay user answers verbatim. Runs survive MCP/chat disconnection. Execution requires explicit factory_execution_policy authorization with reviewed permissions, budget and typed checks. factory_execute returns a durable ID promptly; reuse request_id on retries. Existing authorization remains one slice unless continuation.enabled is explicitly authorized with aggregate limits. A continuation refines, executes and validates integrated milestone criteria. Cross-milestone advancement requires continuation.inter_milestone=true; it is disabled by default. The detached controller prepares only the next eligible milestone and preserves aggregate limits including remediation. checkpoint is a work limit; milestone_ready is validation pending; milestone_closed has a receipt; project_ready_for_validation still needs final project validation. Mandatory subjective reviews require actual human answers. Never act as an LLM supervisor or poll tightly. Use factory_status on user request, factory_inspect execution for evidence, and factory_pause/factory_resume for recovery.'''

PROJECT = {'type': 'string', 'pattern': '^p_[0-9a-f]{16}$'}
TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 8000}


def schema(properties, required=()):
    return {'type': 'object', 'properties': properties, 'required': list(required), 'additionalProperties': False}


TOOLS = {
    'factory_project': ('List, initialize or explicitly select a registered project. Initialization is restricted to locally authorized roots.',
        schema({'action': {'enum': ['list', 'init', 'select']}, 'project': PROJECT,
                'path': {'type': 'string', 'minLength': 1, 'maxLength': 4096},
                'name': {'type': 'string', 'minLength': 1, 'maxLength': 120}}, ['action'])),
    'factory_status': ('Compact authoritative status, blockers, next action and autonomous run progress.', schema({'project': PROJECT})),
    'factory_message': ('Relay the user message verbatim to Factory. Returns promptly; eligible workers continue independently. Reuse request_id when retrying a discovery submission.',
        schema({'project': PROJECT, 'message': TEXT, 'request_id': {'type': 'string', 'minLength': 1, 'maxLength': 128}}, ['message'])),
    'factory_decisions': ('Read pending human decisions with options, recommendations and consequences.',
        schema({'project': PROJECT, 'offset': {'type': 'integer', 'minimum': 0, 'maximum': 100000},
                'limit': {'type': 'integer', 'minimum': 1, 'maximum': 5}})),
    'factory_answer': ('Record a human answer verbatim for a specific pending decision and continue eligible work unless paused.',
        schema({'project': PROJECT, 'decision_id': {'type': 'integer', 'minimum': 1}, 'answer': TEXT}, ['decision_id', 'answer'])),
    'factory_inspect': ('Read the plan, milestones, next_slice, requirement coverage, verification/harness, architecture, revision, ADRs or discovery. Full documents are returned only on explicit inspection.',
        schema({'project': PROJECT, 'view': {'enum': ['architecture', 'revision', 'adrs', 'discovery', 'plan', 'milestones', 'next_slice', 'requirements', 'verification', 'refinement']}, 'slice_id': {'type': 'string', 'minLength': 1, 'maxLength': 64}}, ['view'])),
    'factory_pause': ('Persist a cooperative pause. The current worker call may finish; the controller starts no subsequent call.', schema({'project': PROJECT})),
    'factory_resume': ('Start or resume a durable run until input, a blocker, a limit, a pause or the boundary of implemented phases. Idempotent while active.', schema({'project': PROJECT})),
}
from .execution_contract import POLICY_SCHEMA, VERIFICATION_SCHEMA
TOOLS['factory_execution_policy'] = ('Explicitly authorize bounded execution in the registered Git repository. Bind typed verification definitions to the current plan. No arbitrary commands. Does not start a worker.',
    schema({'project': PROJECT, 'policy': POLICY_SCHEMA, 'verification': VERIFICATION_SCHEMA}, ['policy', 'verification']))
TOOLS['factory_execute'] = ('Start authorized execution and return promptly. Default: one prepared slice. Explicit continuation policy: execute and close milestones within persistent limits; inter_milestone=true authorizes automatic advancement. Reuse request_id on retries.',
    schema({'project': PROJECT, 'request_id': {'type': 'string', 'minLength': 1, 'maxLength': 128}}, ['request_id']))
TOOLS['factory_inspect'][1]['properties']['view']['enum'].append('execution')
TOOLS['factory_pause'] = ('Persist pause; implementation requests runtime interruption and remains pause_requested until the active turn/process stops. Prevents new attempts.', schema({'project': PROJECT}))
READ_ONLY = {'factory_status', 'factory_decisions', 'factory_inspect'}
OUTPUT_SCHEMA = schema({'ok': {'type': 'boolean'}, 'data': {}, 'error': {'type': 'object'}}, ['ok'])


class FactoryTools:
    def __init__(self, service):
        self.service = service

    def call(self, name, arguments):
        try:
            from jsonschema import Draft202012Validator
            if name not in TOOLS:
                raise FactoryError('unknown_tool', 'Unknown Factory operation')
            errors = list(Draft202012Validator(TOOLS[name][1]).iter_errors(arguments))
            if errors:
                raise FactoryError('invalid_arguments', 'Arguments do not match the closed tool schema',
                                   details={'field': '.'.join(map(str, errors[0].path))})
            args = dict(arguments)
            if name == 'factory_project':
                action = args.pop('action')
                if action == 'list' and not args:
                    result = self.service.projects()
                elif action == 'init' and set(args) <= {'path', 'name'} and 'path' in args:
                    result = self.service.initialize_project(**args)
                elif action == 'select' and set(args) == {'project'}:
                    result = self.service.select_project(**args)
                else:
                    raise FactoryError('invalid_arguments', 'init needs path; select needs project; list takes no other arguments')
            elif name == 'factory_status':
                result = self.service.get_status(**args)
            elif name == 'factory_message':
                result = self.service.submit_user_message(**args)
            elif name == 'factory_decisions':
                result = self.service.list_pending_decisions(**args)
            elif name == 'factory_answer':
                result = self.service.answer_decision(**args)
            elif name == 'factory_inspect':
                if ('slice_id' in args) != (args['view'] == 'refinement'):
                    raise FactoryError('invalid_arguments', 'refinement needs slice_id; other views do not accept it')
                result = self.service.inspect(**args)
            elif name == 'factory_pause':
                result = self.service.pause(**args)
            elif name == 'factory_resume':
                result = self.service.resume(**args)
            elif name == 'factory_execution_policy':
                result = self.service.configure_execution(**args)
            elif name == 'factory_execute':
                result = self.service.execute_next_slice(**args)
            return {'ok': True, 'data': result}
        except FactoryError as exc:
            return {'ok': False, 'error': {'code': exc.code, 'message': str(exc)[:1000], 'details': exc.details}}
        except (WorkflowError, OSError, sqlite3.Error) as exc:
            return {'ok': False, 'error': {'code': 'factory_operation_failed', 'message': str(exc)[:1000], 'details': {}}}


def build_server(service=None):
    from mcp.server.lowlevel import Server
    from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool, ToolAnnotations
    adapter = FactoryTools(service or FactoryService())

    async def list_tools(context, params):
        return ListToolsResult(tools=[Tool(name=name, description=description, inputSchema=inputs,
            outputSchema=OUTPUT_SCHEMA, annotations=ToolAnnotations(readOnlyHint=name in READ_ONLY,
                destructiveHint=False, openWorldHint=False)) for name, (description, inputs) in TOOLS.items()])

    async def call_tool(context, params):
        result = await asyncio.to_thread(adapter.call, params.name, params.arguments or {})
        return CallToolResult(content=[TextContent(text=json.dumps(result, ensure_ascii=False))],
                              structuredContent=result, isError=not result['ok'])

    return Server('software-factory', version='0.1.0', instructions=INSTRUCTIONS,
                  on_list_tools=list_tools, on_call_tool=call_tool)


async def serve(service):
    from mcp.server.stdio import stdio_server
    server = build_server(service)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main():
    parser = argparse.ArgumentParser(description='Software Factory MCP (stdio)')
    parser.add_argument('--home', help='Trusted local registry directory; never a tool argument')
    args = parser.parse_args()
    asyncio.run(serve(FactoryService(Registry(args.home))))


if __name__ == '__main__':
    main()
