# Architecture Decision Records

**Format:** Status · Context · Decision · Consequences · Alternatives.

**Rules:**
- An accepted ADR is never edited in substance.
- To change a decision, add a new ADR that supersedes it.
- When an ADR and the [architecture docs](../architecture/README.md) disagree, the ADR wins.

| # | Decision | Status |
|---|---|---|
| [0001](0001-modular-monolith.md) | Modular monolith, `api` + `worker` processes | Accepted |
| [0002](0002-postgres-source-of-truth.md) | Postgres is the source of truth; Redis is ephemeral | Accepted |
| [0003](0003-tenant-isolation.md) | `organization_id` everywhere, composite FKs, tenancy guard, RLS by Phase 9.5 | Accepted |
| [0004](0004-database-roles.md) | DB roles: migrator / api / worker / system | Accepted |
| [0005](0005-identity-and-tokens.md) | Own auth, principals, JWT without org or roles, rotating refresh tokens | Accepted |
| [0006](0006-rbac-six-roles.md) | Six roles, including `operator` | Accepted |
| [0007](0007-immutable-agent-versions.md) | Immutable AgentVersions, append-only deployments, rollback semantics | Accepted |
| [0008](0008-declarative-spec-and-model-client.md) | Declarative spec compiled to LangGraph; own ModelClient | Accepted |
| [0009](0009-execution-state-machine.md) | Execution state machine | Accepted |
| [0010](0010-toolgateway-and-idempotency-ledger.md) | Single ToolGateway; tool-call idempotency ledger | Accepted |
| [0011](0011-credentials-by-reference.md) | Credentials by reference; Secrets Manager; IAM split | Accepted |
| [0012](0012-checkpoint-protection.md) | Checkpoint protection via envelope encryption with a vetted library | Accepted (library choice provisional) |
| [0013](0013-psycopg3-single-driver.md) | psycopg 3 as the single Postgres driver | Accepted |
| [0014](0014-approvals-action-hash.md) | Approvals bound to an action hash; single approver in MVP | Accepted |
| [0015](0015-mcp-remote-only-pinned.md) | Remote-only, hash-pinned MCP | Accepted |
| [0016](0016-traces-and-evals-minimal.md) | Span-based traces with redaction; minimal evals | Accepted |
| [0017](0017-cost-at-write-time.md) | Cost computed at write time from effective-dated prices | Accepted |
| [0018](0018-data-erasure.md) | Data erasure via `subject_key` + nullable content | Accepted |
| [0019](0019-memory-ownership-and-pgvector.md) | Memory owned by Agent; pgvector only when justified | Accepted (pgvector version provisional) |
| [0020](0020-async-execution-postgres-queue.md) | Async execution on a Postgres queue from Phase 3 | Accepted |
| [0021](0021-agent-spec-schema-v1.md) | Agent spec schema v1: `llm` / `router` / `end`, acyclic, JMESPath, JCS hash | Accepted |
