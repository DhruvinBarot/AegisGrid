# ADR-0010: Single ToolGateway and the tool-call idempotency ledger

**Status:** Accepted 2026-10-26

## Context

Agents call external systems based on what an LLM decides, and the LLM's output is untrusted. Execution is at-least-once, so a retried call could repeat an external side effect.

## Decision

### The gateway

Every tool, MCP, HTTP and DB call goes through `ToolGateway`. For each call it runs these steps in order:

1. Authorize against `version.bindings`.
2. Validate the arguments against the pinned schema and policy.
3. Approval and taint check. Before Phase 7 this step **denies** non-read tools when the environment is protected or the execution is tainted.
4. Record the call's intent in the ledger.
5. Resolve the binding and secret, and register the secret with the scrubber.
6. Apply the egress and SSRF guard: resolve-then-pin, a blocklist, and redirects re-checked.
7. Make the call.
8. Post-process the result: enforce limits, strip unsafe content, scrub secrets, and taint the output.
9. Record the ledger result and the span.

### The ledger

The ledger is the `tool_calls` table.

- Key: `sha256(execution_id ‖ node_id ‖ call_index)`, unique per org.
- The intent row is written before the call.
- If the call is already `completed`, the gateway returns the recorded result instead of calling again.
- If the same key arrives with a different `args_hash`, the call is marked `unknown`.

### Side-effect classes

Each `ToolDefinition` declares one:

| Class | Retry behavior |
|---|---|
| `read` | Retried freely |
| `idempotent_write` | Retried with the same key; sends `Idempotency-Key` where the target supports it |
| `non_idempotent_write` | **Never auto-retried** when the outcome is ambiguous. The call is marked `unknown`, the execution gets `needs_attention`, and a human decides |

## Consequences

- There is one enforcement point for security, approvals, idempotency and tracing.
- Each tool call must be its own checkpointed step (see ADR-0008).

## Alternatives rejected

- **Letting nodes call out directly:** this leaves no enforcement point.
- **Blind retries:** these duplicate emails and payments.
