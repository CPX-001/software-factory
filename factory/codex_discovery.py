"""Adapter for the installed official Codex SDK and the existing ChatGPT login."""

import json
import tempfile

from .discovery_contract import INSTRUCTIONS, RESPONSE_SCHEMA
from .workflow import WorkflowError


class CodexDiscovery:
    def __init__(self, model="gpt-5.6-terra"):
        self.model = model

    def respond(self, context):
        try:
            from openai_codex import ApprovalMode, Codex, CodexConfig, Sandbox
        except ImportError as exc:
            raise WorkflowError("Official Codex SDK is missing; run with .venv/bin/python or activate .venv") from exc
        try:
            # A fresh ephemeral thread is the context boundary. No project files or old
            # SDK threads are needed. Preserve the existing CODEX_HOME/authentication.
            with tempfile.TemporaryDirectory(prefix="factory-discovery-") as cwd:
                config = CodexConfig(
                    cwd=cwd,
                    config_overrides=('forced_login_method="chatgpt"', 'model_provider="openai"'),
                    env={"OPENAI_API_KEY": "", "CODEX_API_KEY": ""},
                )
                with Codex(config=config) as codex:
                    account = codex.account().account
                    if account is None or account.root.type != "chatgpt":
                        raise WorkflowError("Discovery requires the existing Codex/ChatGPT login; API-key fallback is disabled")
                    thread = codex.thread_start(
                        model=self.model, model_provider="openai", cwd=cwd, ephemeral=True,
                        sandbox=Sandbox.read_only, approval_mode=ApprovalMode.deny_all,
                        base_instructions=INSTRUCTIONS,
                    )
                    result = thread.run(json.dumps(context, ensure_ascii=False), output_schema=RESPONSE_SCHEMA)
                    if getattr(result.status, "value", result.status) != "completed":
                        raise WorkflowError("Codex did not complete the turn. The input is saved; rerun discovery to retry.")
                    return json.loads(result.final_response)
        except WorkflowError:
            raise
        except Exception as exc:
            # No API client, key fallback, or automatic retry that would consume quota.
            raise WorkflowError(f"Codex discovery failed ({type(exc).__name__}): {exc}. The input is saved; rerun discovery to retry.") from exc
