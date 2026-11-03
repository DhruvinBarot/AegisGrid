# ADR-0009: Execution state machine

**Status:** Accepted, 2026-10-26

## Context

Executions need a lifecycle that the UI, API, worker and reaper all read the same way. The smallest set of states is also the safest.

## Decision

**States:** `queued`, `running`, `waiting_for_approval`, `succeeded`, `failed`, `cancelled`, `timed_out`.

**Terminal states:** `succeeded`, `failed`, `cancelled` and `timed_out`. Once an execution reaches one of these, it is frozen; a trigger rejects any transition out of a terminal state.

**Transitions:**
- `queued → running` happens when a worker claims the execution.
- `queued → cancelled` happens when it is cancelled before any worker claims it.
- From `running`, an execution can move to `waiting_for_approval` or to any terminal state.
- From `waiting_for_approval`, it can go back to `running` (on resume) or end as `cancelled` or `timed_out`.
- Expiry while waiting counts as a rejection and resumes the run on its reject path.
- `running → queued` happens only through infrastructure: lease expiry, or retry with backoff.

**Not states:**
- **`retrying`** is an attribute. It is derived from `job.attempt` and retry spans.
- **Cancel** is a flag, `cancel_requested_at`. The first conditional terminal write wins.
- **`needs_attention`** is a boolean on a failed execution. It means the run ended with an ambiguous non-idempotent write.

**Step status:** steps have their own statuses, stored on spans. They never drive the execution's status directly.

## Consequences

- Each transition is a single conditional UPDATE, so races between cancel, completion and reaping resolve deterministically.

## Alternatives rejected

- **A `retrying` state:** doubles the number of transition edges without adding any user value.
- **Cancellation as a state:** introduces races with completion.
