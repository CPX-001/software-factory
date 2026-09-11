---
name: software-factory
description: Develop an idea and continue a Factory-managed project through a persistent, adaptive Codex workflow. Use for project conversations, ongoing work, status and pause/resume. Do not use Factory recursively to modify Factory itself.
---

Factory keeps project memory and runs normal Codex independently of the chat. The conversation
is the user's interface, not a phase supervisor. Codex chooses the process for the product,
uses its normal tools and installed skills, and maintains useful project documents as work evolves.

- Initialize with `factory_project action=init` at a user-supplied path or the reliable conversation
  workspace. Ask for a path only if neither is known. New projects use `adaptive` automatically.
  Never infer workspace from MCP cwd. Pin the returned project ID in subsequent calls.
- Relay substantive user input faithfully with `factory_message`, using a stable `request_id`
  for transport retries. This starts eligible work and accepts steering during a run. Use
  `factory_answer` for an identified question; conversational replies can use message.
  Present the worker's questions and findings naturally. Do not invent a parallel questionnaire
  or mandatory approvals. Architecture acceptance can be implicit in the conversation.
- The worker chooses research, design, implementation and appropriate checks. Focus shifts
  smoothly and earlier decisions remain revisable. No fixed phase sequence, language, test
  framework or mandatory test count. Do not impose historical pilot rules. The worker chains
  steps without 'continue' prompts and stops at agreed scope, parking future improvements.
  Genuine unresolved choices or external access needs can require user input.
- Use `factory_status` for focus, message, tasks, checks, questions and accumulated usage.
  `factory_inspect process` or `memory` shows context; `execution` shows runtime observations;
  `plan`, `architecture` and `verification` show saved views. During a conversation, allow a
  reasonable wait and consult status to relay the worker's reply. Do not repeatedly call resume
  or execute to sustain work: the detached controller does that. Closing the chat or MCP does
  not stop the run. Background findings are available on the next status consultation; do not
  promise unsolicited App notifications.
- Pause via `factory_pause`; preserve it until explicit resume. `pause_requested` means the
  runtime has not yet confirmed interruption. `factory_resume` recovers interrupted work;
  repeating it on completed work does not restart the project. A new substantive message can
  extend or revise a project, preserving previous checkpoints and reports.
- Model and reasoning inherit host Codex configuration. A temporary selection in this App chat
  is not automatically transmitted by MCP. If the user specifies settings, use `factory_project
  action=configure` with `settings.model`, `effort` or `service_tier`; null restores inheritance.
  Optional `max_calls`, `max_tokens`, `max_seconds` are cumulative. Routine adaptive work needs
  no execution-policy grant. Do not change global settings or billing provider.
- `completed` means Codex reports agreed work complete. Distinguish actual checks, mock
  integrations and unavailable checks. This is not independent Factory certification,
  deployment or universal security assurance. The local report records observed Git state,
  which can include uncommitted work; do not call it a verified immutable commit.
- Projects without `workflow: adaptive` retain their historical contracts. Read
  [references/verified-workflow.md](references/verified-workflow.md) only for those projects.
  Preserve their receipts and existing authorizations; do not convert them silently.

When modifying Software Factory itself, use ordinary Codex tools on its repository rather
than sending that work through Factory. Keep product experiments in separate disposable repos.
