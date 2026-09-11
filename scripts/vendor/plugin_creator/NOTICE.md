# Bundled Codex plugin helpers

These four unmodified helper scripts were copied from the installed OpenAI Codex
`plugin-creator` skill on 2026-09-11: `create_basic_plugin.py`,
`read_marketplace_name.py`, `update_plugin_cachebuster.py`, and
`identifier_validation.py`.

They implement the same personal marketplace and cache update flow used during
development. Bundling them lets this private installer run on a machine without
the authoring skill. They use only the Python standard library. Refresh them
together when adopting a new Codex plugin installation contract.
