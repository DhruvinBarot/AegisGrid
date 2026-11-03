# ADR-0007: Immutable AgentVersions and append-only deployments

**Status:** Accepted, 2026-10-26

## Context

A production execution must stay explainable after its agent changes. We also need rollback, and we need to promote one artifact across environments.

## Decision

- **Agent** is the identity, with a single mutable draft (`draft_spec`, `draft_rev`).
- **Publish** creates an **AgentVersion**:
  - The spec is canonicalized with RFC 8785 JCS and `content_hash` is computed. The hash covers `spec_schema_version`.
  - The row gets `version_no = n+1` and is then **immutable**. A trigger rejects UPDATE and DELETE.
  - The snapshot contains: the prompt, the pinned model, the workflow, **copies** of tool schemas with their hashes, MCP pins, and the memory, approval, retry, timeout, output and eval policies.
- **What a version only references, never copies:** credentials, per-environment endpoints (by logical integration name), prices and budgets.
- **Deployment** appends a row to `agent_deployments` and moves the pointer in `agent_environment_state`, in one transaction.
  - **Rollback** is a new deployment row pointing at an older version.
  - **Promotion** is deploying the same version into the next environment.
- **Rollback does not undo external side effects.** The UI states this explicitly.

## Consequences

- Published specs are permanent. Every future runtime must read spec v1, so `spec_schema_version` and an upgrader path are required.
- Executions record `agent_version_id`, `deployment_id` and binding revisions, which makes them explainable.

## Alternatives rejected

- **Mutable agent config:** history becomes unexplainable.
- **Snapshotting credentials or endpoints:** frozen secrets end up in history, and versions can't be ported across environments.
