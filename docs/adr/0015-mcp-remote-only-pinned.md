# ADR-0015: Remote-only, hash-pinned MCP

**Status:** Accepted 2026-10-26

## Context

MCP servers are third-party code. Their tool descriptions can change after review (a rug-pull) or contain injected instructions. A stdio server runs code inside our worker.

## Decision

- **Model:** MCP is an `Integration` with `kind='mcp'`. It gets per-environment bindings and its own CredentialReference.
- **Transport:**
  - Remote servers only (Streamable HTTP over TLS), routed through the gateway's egress/SSRF guard.
  - stdio servers are **disallowed** until a sandbox exists.
- **Trust tiers:** `platform_verified` and `org_remote`.
- **Pinning:**
  - At publish, `tools/list` runs and each tool's name, input schema and description hash are pinned into the AgentVersion.
  - At runtime, any diff from the pinned hashes **blocks** the call and raises an alert.
- **Locked-down behavior:**
  - Server-initiated sampling and elicitation are disabled.
  - User credentials are never passed through to the server.
  - Server output is tainted.

## Consequences

- A server that changes its tool descriptions is caught.
- Legitimate server updates need a new AgentVersion publish to take effect.

## Alternatives rejected

- **Trusting live `tools/list`:** rug-pull risk.
- **Allowing stdio in MVP:** arbitrary code would run in the worker.
