# Agent-Lane Project Guide

This project is an external control layer for durable Codex tasks and optional
agent integrations.

Rules:

- Do not edit upstream agent or Codex source code from this project.
- Keep dependencies minimal; prefer Python standard library.
- Treat `codex app-server` JSON-RPC as the boundary.
- Preserve `run` as create-or-resume, not create-only.
- Keep command output machine-readable JSON for agent consumers.
- Do not store secrets in lane alias files.
- Do not make destructive git or Codex config changes without explicit user
  instruction.

Versioning guidance:

- Treat the timeout/observation-window + durable runner/control-plane behavior
  as a minor version increment (`+0.1.0`, for example `0.4.0` -> `0.5.0`)
  because it changes the core user-facing semantics of long-running Codex lanes.
- Treat small follow-up fixes, polish, diagnostics, and compatibility patches as
  patch increments (`+0.0.1`) unless they materially change lane semantics again.

The first supported provider is `codex`.
