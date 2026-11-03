# ADR-0019: Memory is owned by the Agent; pgvector only when justified

**Status:** Accepted, 2026-10-26. The pgvector version requirement is **provisional**.

## Context

Agents need persistent memory that is isolated per tenant. Semantic retrieval is sometimes useful, but running a separate vector database for the MVP would add operational weight and another place tenant isolation could fail.

## Decision

### Ownership and scope

- `memory_records` is scoped by `(organization_id, agent_id, environment_id, namespace, key)`, plus an optional `subject_key`.
- Memory belongs to the **Agent**, not to an AgentVersion. It survives upgrades and rollbacks.
- The version's memory policy controls what the agent may read and write.
- Production memory is never visible to dev.

### Provenance and quarantine

- Every record stores its source execution, span, version, `trust` level and `expires_at`.
- Records written by tainted executions are marked `untrusted` and excluded from default retrieval.

### Retrieval

- **By default:** exact key, namespace listing, and tsvector full-text search.
- **pgvector (same Postgres instance):** used only when a single scope holds more than a few thousand free-text items and full-text search demonstrably misses. When it is used:
  - the `embedding_model` is recorded,
  - filters go inside the SQL `WHERE`,
  - `hnsw.iterative_scan` is set with `SET LOCAL`.

### Version requirement (provisional)

- pgvector **≥ 0.8.3**.
  - Iterative scan was added in 0.8.0.
  - 0.8.3 (2026-06-17) fixed possible HNSW index corruption during vacuum.
- This must be verified against the exact pinned PostgreSQL/pgvector image and the RDS minor version before it is relied on.
- The compose image tag `pgvector/pgvector:pg17` is currently floating. It will be pinned after that verification.

### Deletion

- A sweeper enforces TTLs and caps.
- Erasure hard-deletes records by `subject_key` (see ADR-0018).

## Consequences

- One database for memory, and tenant isolation stays in SQL.
- Changing the embedding model or dimension later means a re-embedding migration.

## Alternatives rejected

- **A dedicated vector database:** extra operational weight, and a second place tenant isolation could fail.
- **Memory owned by AgentVersion:** it would be lost on every upgrade.
