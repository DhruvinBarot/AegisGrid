# ADR-0014: Approvals bound to an action hash

**Status:** Accepted, 2026-10-26 (resolves validation finding C3)

## Context

Approvals guard sensitive actions. They must not be bypassable, replayable, or approved against stale arguments. A paused run must not hold a worker.

## Decision

- **Pausing.** A gate node, or the gateway, calls `interrupt()`. The runner writes an `approval_requests` row, sets the execution to `waiting_for_approval`, and ends the job, releasing the worker.
- **Action hash.** Each request stores `action_hash = sha256(JCS{org, execution, version, node, tool_call_id, tool_name, tool_schema_hash, args, credential_ref_ids, env})`.
- **Decision transaction.** The decision runs in one transaction:
  1. Lock the request with `FOR UPDATE`.
  2. Check that it is still pending and not expired.
  3. Re-check the approver's permission at that moment.
  4. Enforce separation of duties.
  5. Require the submitted hash to equal the stored hash.
  6. Write the audit event.
  7. Enqueue the resume job.
- **No argument editing.** Approvers cannot change the arguments. To change them, reject the request and let the workflow raise a new one.
- **Resuming.** The worker:
  1. Recomputes the hash.
  2. Consumes the approval exactly once, with a conditional UPDATE on `consumed_at`.
  3. Re-runs gateway policy.
  4. Builds `Command(resume=…)` **only from the approval row**.
- **Expiry.** Requests expire after 24 hours by default (proposed). Expiry counts as a rejection.
- **MVP limits.**
  - One approver per request. Two-approver approval would need an `approval_decisions` child table later.
  - Approvals cover execution actions only. Deployment approvals are not included.

## Consequences

- Bait-and-switch, replay, and forged-resume attacks are structurally prevented.
- LangGraph re-runs an interrupted node from its start, so gates must do nothing with side effects before `interrupt()`.

## Alternatives rejected

- **Approval by id only:** the approved action could differ from the one that runs.
- **Editable arguments:** the approver would be approving something different from what was requested.
