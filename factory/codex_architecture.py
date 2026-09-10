"""Fresh, independent SDK turns with native required skill activation."""

import json
import tempfile

from .architecture_contract import ARCHITECTURE_SCHEMA, INSTRUCTIONS, REVIEW_SCHEMA
from .workflow import WorkflowError
from .codex_worker import worker_overrides


class CodexArchitecture:
    def __init__(self, model='gpt-5.6-terra'):
        self.model = model

    def respond(self, context, *, skill_inputs):
        try:
            from openai_codex import ApprovalMode, Codex, CodexConfig, Sandbox, SkillInput, TextInput
            with tempfile.TemporaryDirectory(prefix='factory-architecture-') as cwd:
                config = CodexConfig(cwd=cwd,
                    config_overrides=worker_overrides(),
                    env={'OPENAI_API_KEY': '', 'CODEX_API_KEY': ''})
                with Codex(config=config) as codex:
                    account = codex.account().account
                    if account is None or account.root.type != 'chatgpt':
                        raise WorkflowError('Architecture requires the existing Codex/ChatGPT login; API-key fallback is disabled')
                    # base_instructions avoids injecting a project-wide skill catalog. Only
                    # selected required references are native skill inputs, not assertions of use.
                    thread = codex.thread_start(model=self.model, model_provider='openai', cwd=cwd,
                        ephemeral=True, sandbox=Sandbox.read_only, approval_mode=ApprovalMode.deny_all,
                        base_instructions=INSTRUCTIONS)
                    inputs = [TextInput(json.dumps(context, ensure_ascii=False))]
                    inputs += [SkillInput(name=s['name'], path=s['path']) for s in skill_inputs]
                    schema = REVIEW_SCHEMA if context['role'].startswith('critic') else ARCHITECTURE_SCHEMA
                    result = thread.run(inputs, output_schema=schema)
                    if getattr(result.status, 'value', result.status) != 'completed':
                        raise WorkflowError('Codex architecture turn did not complete; resume architecture to retry')
                    return json.loads(result.final_response)
        except WorkflowError:
            raise
        except Exception as exc:
            raise WorkflowError(f'Codex architecture failed ({type(exc).__name__}): {exc}') from exc
