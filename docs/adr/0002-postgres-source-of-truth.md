# ADR-0002: PostgreSQL is the source of truth; Redis is ephemeral

**Status:** Accepted, 2026-10-26

## Context

The platform's durable state spans tenants, versions, jobs, workflow checkpoints, approvals, memory, traces, usage and audit. Each additional durable store would add another failure mode and another place where data must be kept consistent.

## Decision

- **PostgreSQL 17 holds every durable record:**
  - the job queue
  - LangGraph checkpoints
  - memory, including vectors if they are ever needed
- **Redis is limited to:**
  - cache
  - rate limits
  - pub/sub for the live trace view and cancel nudges
  - short locks

  Redis can be flushed at any time without losing data, and `/readyz` does not depend on it.
- **Object storage (S3)** holds only payloads larger than 64 KB, which are referenced from Postgres rows.

## Consequences

- One backup and restore story, one consistency model, and atomic enqueue.
- Postgres becomes the main point of contention under load. This is mitigated by:
  - batched writes
  - partitions
  - payload offload
  - the Phase 5 benchmark gate

## Alternatives rejected

- **Celery/RQ on Redis:** dual writes and lost jobs when Redis restarts.
- **Kafka:** operational weight that isn't needed yet.
- **A separate vector database:** see ADR-0019.
