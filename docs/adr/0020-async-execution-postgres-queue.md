# ADR-0020: Async execution on a Postgres queue from Phase 3

**Status:** Accepted, 2026-10-26. Resolves validation findings C7, C11, C12 and C14.

## Context

Agent runs last seconds to minutes and can pause for approval. A synchronous design would have to be rewritten later. The queue needs to be durable and atomic with the execution record.

## Decision

### Execution model

- `POST …/executions` returns **202** from Phase 3 onward. `?wait=N` (N ≤ 30) offers a sync-style wait:
  - It returns 200 with the result, or 202 if the run is still going.
  - It waits on Redis pub/sub and falls back to polling Postgres.
  - Concurrent waits are capped per org.
- Enqueue happens in one transaction: insert the execution, insert the job, `NOTIFY`.
- `Idempotency-Key` is unique per (org, principal) and stored with a request hash. The same key with a different body returns 422.

### Claiming and leases

- Claims use `FOR UPDATE SKIP LOCKED` and run as `aegis_system`. Processing then runs as `aegis_worker` with `app.org_id` set.
- A lease lasts 60 s. Each worker sends one batched heartbeat every 15 s. Every write is fenced on `lease_owner`.
- Delivery is at-least-once. Duplicate side effects are prevented by checkpoint resume plus the tool ledger (ADR-0010).
- LISTEN/NOTIFY is used only as a hint. Workers also poll every 1–2 s.

### Reaper

The reaper runs under an advisory lock.

| Phase | Behaviour |
|---|---|
| **3** | Expired lease → requeue |
| **5** | Adds backoff with jitter, `dead`, `worker_lost`, deadline timeouts and approval expiry |

### Capacity

- MVP target: 10 executions/s with 500 concurrent.
- The **Phase 5 benchmark gate** requires p95 claim latency under 1 s and no false lease expiries.
- The expected first bottleneck is write volume from checkpoints and spans, not claims.
- Mitigations: batched spans, rollups and heartbeats, plus pruning of finished jobs.

### When to move to SQS

Move to SQS (behind `JobQueue`, keeping the outbox row) if any of these happen:

- sustained claims exceed ~1k/s,
- queue churn hurts primary latency,
- multi-region becomes necessary.

## Consequences

- No dual writes, and no lost jobs when Redis restarts.
- Executions waiting on approval hold no worker.
- Postgres write load must be watched (ADR-0002).

## Alternatives rejected

- **Sync-first:** would force a runtime rewrite later.
- **Celery or RQ:** dual writes, and Redis would become durable state.
