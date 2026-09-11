"""Normal Codex kernel, with a compact checkpoint instead of file proposals."""
from pathlib import Path

from .codex_execution import CodexExecution
from .codex_worker import worker_overrides


def obj(properties):
    return {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}


STRING = {'type': 'string'}
STRINGS = {'type': 'array', 'items': STRING}
CHECKPOINT = obj({
    'focus': STRING,
    'status': {'enum': ['continue', 'waiting_for_user', 'completed']},
    'message': STRING, 'objective': STRING, 'memory': STRING, 'next_step': STRING,
    'tasks': {'type': 'array', 'items': obj({'id': STRING, 'title': STRING,
        'status': {'enum': ['pending', 'active', 'done', 'deferred']}})},
    'checks': {'type': 'array', 'items': obj({'id': STRING, 'description': STRING,
        'required': {'type': 'boolean'},
        'status': {'enum': ['pending', 'passed', 'failed', 'unavailable', 'not_applicable']},
        'evidence': STRING})},
    'questions': {'type': 'array', 'items': obj({'question': STRING, 'reason': STRING})},
    'revisions': {'type': 'array', 'items': obj({'check_id': STRING, 'reason': STRING})},
    'documents': STRINGS, 'deferred': STRINGS,
})

INSTRUCTIONS = '''You are Codex working on the user's project through Software Factory.
Use your normal tools, installed skills and project instructions to do the work directly.
Factory supplies durable memory, user messages and a suggested next step. They are context,
not a fixed pipeline. Research when useful, discuss feasibility objectively, develop the
scope/MVP, design progressively, implement and check the actual product. Choose the process,
technologies, skills, granularity and verification appropriate to THIS project. Focus labels
are descriptive: revisit product decisions, architecture, UX, plans and implementation freely.
There is NO mandatory architecture approval, phase sequence, language, test framework, test
count, critic pass or test gate. Do not ask permission for ordinary work already requested.
Ask focused questions only for consequential unresolved user choices or missing external
access (accounts, credentials, etc.). Do not put secrets into checkpoints or harness documents.
Use waiting_for_user with those questions instead of an interactive request_user_input tool.
Do not ask the user to confirm facts that your tools can establish.

Perform a useful coherent amount of work, then return the checkpoint schema. The controller
will immediately give you the next step while status is continue; you need not ask 'continue'.
The checkpoint is a complete updated snapshot, not a delta. Carry forward existing checks
using their exact IDs, descriptions and required flags when only updating their results.
Do not rename a check to describe the next step: add a new check or record an intentional
revision with its reason. Preserve settled project context when extending an existing product.
Use completed when the agreed scope is done; park optional improvements in deferred. Stop
improving at that point. Tests/checks are chosen by you for the project and can be empty when
appropriate. Preserve checks already committed to: fix failures or explain the genuine blocker;
never conceal failed/unavailable checks or delete required checks to manufacture completion.
If new requirements or a justified design change require revising a previously chosen check,
record its check_id and the substantive reason in revisions. This is not an approval gate;
the purpose is to keep changes visible and preserve their history, not to freeze a design.
Check results are your reports, not independent Factory certification. Distinguish mock and
real integrations. Describe commands/evidence actually used. Completed is not project_verified.

Maintain concise repo documentation such as AGENTS.md, ARCHITECTURE.md, a spec, plan or design
notes when useful. Update affected documents as decisions evolve, without bureaucratic phase
handoffs or rewriting everything. Keep memory compact (under 12000 characters): settled scope,
important decisions/reasons, relevant file references, discoveries and what the next step needs.
Long details belong in project files. A fresh step gets this memory rather than all transcripts.
Do not read or modify .factory internal state, invoke Software Factory recursively or delegate
control to it. Work in the supplied project directory with normal Codex tools. Do not include
Factory internals in the product. User changes/messages may arrive between checkpoints: incorporate
them, including scope corrections. A recovered incomplete turn may have already edited files;
inspect the current product before repeating operations. Respect any pause requested by Factory.
'''


class CodexAdaptive(CodexExecution):
    instructions = INSTRUCTIONS
    result_schema = CHECKPOINT
    monitors_quota = False

    def __init__(self, project, settings=None):
        from openai_codex import Codex, CodexConfig
        self.project = Path(project)
        self.policy = settings or {}
        self.skill_inputs = []  # Normal Codex discovers the installed catalog itself.
        self.on_item = lambda item: None
        self.runtime_info = {'configuration': 'normal_codex_tools', 'model': self.policy.get('model'),
                             'effort': self.policy.get('effort'), 'settings_source': 'codex_config_with_project_overrides'}
        self.codex = Codex(config=CodexConfig(cwd=str(self.project), config_overrides=worker_overrides()))
        self.runtime_info['server_version'] = self.codex.metadata.serverInfo.version

    def thread_options(self, *, starting):
        from openai_codex import ApprovalMode, Sandbox
        # Keep the normal Codex base instructions and catalog; add only the continuity layer.
        args = {'cwd': str(self.project), 'sandbox': Sandbox.full_access,
                'approval_mode': ApprovalMode.deny_all, 'developer_instructions': self.instructions}
        if self.policy.get('model'):
            args['model'] = self.policy['model']
        return args

    def turn_options(self):
        from openai_codex.generated.v2_all import ReasoningEffort
        args = {'output_schema': self.result_schema}
        if self.policy.get('effort'):
            args['effort'] = ReasoningEffort(self.policy['effort'])
        return args

    def observe_item(self, item):
        # Save provenance/exit status, not command output, credentials or full transcripts.
        value = item.model_dump(by_alias=True, mode='json')
        allowed = ('id', 'type', 'status', 'exitCode', 'durationMs', 'tool', 'server')
        record = {key: value[key] for key in allowed if key in value}
        if value.get('type') not in ('agentMessage', 'reasoning'):
            self.on_item(record)

    def close(self):
        if self.codex is not None:
            self.codex.close()
