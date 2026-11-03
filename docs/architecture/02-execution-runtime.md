# 02 · Execution, Runtime, Tools, Memory, Approvals

## Execution lifecycle

```
            ┌──────────── cancel ────────────┐
 queued ──claim──▶ running ◀──resume── waiting_for_approval
   │               │  ▲                         │
   │ cancel        │  └─ lease expiry / backoff │ cancel / expiry (= reject)
   ▼               ▼     (infra-only requeue)   ▼
 cancelled   succeeded | failed | cancelled | timed_out   (terminal, frozen)
```

**States:**

- The seven states are `queued`, `running`, `waiting_for_approval`, `succeeded`, `failed`, `cancelled` and `timed_out`.
- `retrying` is an **attribute**, not a state. Each job has an attempt count, and retries show up as spans. The UI works out "retrying" from those.
- `needs_attention` is a boolean on a failed execution. It means a non-idempotent write ended ambiguously.
- Step status lives on spans: `pending`, `running`, `succeeded`, `failed`, `skipped`, `waiting`, `cancelled`. The runtime sets execution status itself; step status never drives it directly.

**Cancel:**

- Cancel is a flag (`cancel_requested_at`), not a state.
- The worker honours it between nodes.
- If cancel and completion race, the first conditional `UPDATE … WHERE status NOT IN (terminal)` wins.
- A trigger rejects any transition out of a terminal state.

### Execution snapshot

Every execution records:

- **What ran:** `agent_id`, `agent_version_id`, `environment_id`, `deployment_id`.
- **What it was wired to:** `resolved_bindings` (integration → binding revision id) and `credential_ref_ids` (ids only).
- **Inputs:** `input` (or an S3 pointer), `input_hash`, and `subject_key`, the caller's pseudonymous id for the data subject (see [erasure](03-security-and-privacy.md#data-erasure)).
- **Lineage:** `kind` (normal, retry, replay or eval), the parent and root execution ids, `triggered_by`, and the principal.
- **Request identity:** `idempotency_key`, `request_id`, `trace_id`.
- **Runtime:** `runtime_version`, made of the image git SHA, the spec-interpreter version and the LangGraph version.
- **Progress and outcome:** `deadline_at`, `attempt`, `output`, `error {code, class, retriable}`, `cost_total`, `erased_at`.

**Invariant:** content columns (`input`, `output`, error message) are nullable and erasable. Metadata is never erased.

### Reproducibility

The guarantee is that every execution is **explainable**: you can always see the exact version, configuration, prompts and tool outputs that produced it. It is **not** bit-reproducible, because LLMs are non-deterministic and models get retired.

| Operation | What it does |
|---|---|
| **Replay** | Walks the graph again, substituting the LLM and tool outputs recorded in spans. Side effects are blocked. |
| **Re-execution** | Starts a new execution linked to its parent. It uses the same version and is expected to produce different results. |

## Async execution strategy

**Creating an execution:**

1. **Enqueue.** One transaction inserts the `executions` row (status `queued`) and its `jobs` row, then sends `NOTIFY jobs`. There is no dual write.
2. **Idempotency.** `Idempotency-Key` is unique per org and principal, and the request hash is stored with it.
   - Same key, same body: returns the existing execution.
   - Same key, different body: returns **422**.

**Claiming and holding a job:**

- **Claim:**
  ```
  UPDATE jobs … WHERE id = (SELECT … WHERE state='queued' AND run_after<=now()
                            ORDER BY run_after FOR UPDATE SKIP LOCKED LIMIT 1)
  ```
  - The claim sets `lease_owner`, a 60-second lease, and `attempt+1`.
  - It runs as `aegis_system`. Processing then continues as `aegis_worker` with `app.org_id` set.
- **Heartbeat.** One batched update per worker every 15 s: `UPDATE jobs SET lease_expires_at=… WHERE lease_owner=$me`.
- **Fencing.** Every worker write includes `lease_owner = $me`. A zombie worker's late writes therefore match zero rows, and it stops.

**Background maintenance:**

- **Reaper.** Runs on whichever worker holds an advisory lock.
  - Expired leases go back to `queued`. Phase 3 does only this.
  - Phase 5 adds backoff with jitter, the `dead` state, `worker_lost` failures, deadline timeouts and approval expiry.
- **Wakeups.** LISTEN/NOTIFY on a dedicated, unpooled connection is only a latency hint. Workers also poll every 1–2 s.

**Failure and control:**

- **Crash mid-step.** The next claim resumes from the last checkpoint, so completed nodes do not re-run. The node that was in flight does re-run, and its tool calls hit the ledger.
- **Timeouts.** Each node runs under `asyncio.timeout`. The wall clock is enforced through `deadline_at`, checked both between nodes and by the reaper. If the worker task cannot be cancelled, the process exits and ECS restarts it.
- **Cancellation.** A cooperative check runs between nodes. A Redis pub/sub nudge interrupts long waits. Postgres stays authoritative.
- **Retries.**
  - Node retries cover transient errors and happen inside the run.
  - Job retries cover infrastructure failures only.
  - A user-initiated retry is a *new* execution with `parent_execution_id` set.

**Sync calls.** `POST …/executions?wait=N`, with N ≤ 30, waits on Redis pub/sub for the terminal state, falling back to a Postgres poll.
- Returns 200 with the result if the run finishes in time, otherwise 202.
- Concurrent waiters are capped per org.

**Capacity model** (to be benchmarked; [ADR-0020](../adr/0020-async-execution-postgres-queue.md)):

- The claim rate is not the limit. The limit is how many rows each execution writes.
- An execution of about 10 nodes writes roughly 60–80 rows.

| Load | Executions/s | Concurrent | Row writes/s |
|---|---|---|---|
| MVP target | 10 | 500 | ~800 |
| At 100× | — | — | ~80k (the primary saturates on checkpoints and traces before the queue becomes the bottleneck) |

- **Phase 5 benchmark gate:**
  - 10 executions/s with 500 concurrent
  - p95 claim latency under 1 s
  - no false lease expiries
- **Move to SQS** only when one of these holds:
  - sustained claims exceed ~1k/s, or
  - queue churn hurts primary latency, or
  - multi-region is needed.

  The move happens behind the `JobQueue` protocol.

## LangGraph runtime

**Spec format:**

- A JSON document: `schema_version`, `state_schema` (JSON Schema), `entry`, `nodes[{id, type, config, retry, timeout_s}]` and `edges[{from, to, when?}]`.
- Router conditions use **JMESPath**. There is no `eval` and no user-supplied Python.
- Loops require `max_iterations`. Every run also has a global `max_steps` and `max_cost`.
- **State-schema rule** ([ADR-0012](../adr/0012-checkpoint-protection.md)): every top-level state channel must be a container (dict, list or messages).
  - Primitive top-level channels are allowed only for reserved, non-sensitive runtime fields.
  - The reason: LangGraph's Postgres saver stores primitive channels inline as plaintext JSONB, bypassing the serializer.
  - No content ever goes into the run `config`, because the saver also copies config values into plaintext checkpoint metadata.

**Agentic tool loop.** Compiled as an `llm` node and a `tool` node in a loop. Every tool execution is therefore its own checkpointed super-step.

**Compile.** The spec compiles to a `StateGraph`, cached in a per-process LRU keyed by `(content_hash, runtime_version)`. Compilation validates:
- reachability
- bounded loops
- the state-schema rule
- that every binding is present

**Checkpointer:**

- `langgraph-checkpoint-postgres` with `thread_id = execution_id`.
- It uses psycopg 3, the single DB driver ([ADR-0013](../adr/0013-psycopg3-single-driver.md)), with its own connection pool and the `runtime` schema.
- No transaction is ever held open across an LLM call.

**Approvals:**

1. An `approval_gate` node, or the gateway inside a `tool` node, calls `interrupt(payload)`.
2. The runner catches it, writes the ApprovalRequest, sets the execution to `waiting_for_approval` and finishes the job.
3. On resume, `Command(resume=…)` is built from the approval row, never from client input.

LangGraph re-runs the interrupted node from its beginning. Gates must therefore have no side effects before `interrupt()`, and the tool ledger absorbs re-execution.

**Models** ([ADR-0008](../adr/0008-declarative-spec-and-model-client.md)):

- Our own thin `ModelClient.generate(messages, tools, schema, params) → ModelResult`. The result has normalised usage, a finish reason and a raw reference.
- LangGraph is used only for the graph, checkpointing and interrupts.

**Structured output:**

- Uses the provider's native JSON-schema mode or tool-forced mode.
- Output is validated with Pydantic or jsonschema.
- One bounded repair retry is allowed. After that the node fails with `output_invalid`.

**Tracing.** A `@traced_node` decorator opens and closes a span. Spans are batch-written when a node ends, then a "span updated" message goes to Redis for live views.

## Tool and MCP architecture

**Abstraction.** An `Integration` (http, db, builtin or mcp; browser later) has a per-environment `IntegrationBinding` (config plus `credential_ref_id`) and exposes `ToolDefinition`s. A version binds `(logical_integration, tool_name, schema_hash)`.

**ToolGateway pipeline.** Every call goes through these steps in order:

1. **Authorize.** The tool must be in `version.bindings` with a matching schema hash. Hallucinated names are rejected.
2. **Validate arguments** against the pinned schema and policy constraints: allowed domains, allowed recipients, limits.
3. **Approval check.** Required by policy, by risk plus taint, or by an unknown outcome.
   - **Before Phase 7**, the gateway **denies** non-read tools in protected environments, and in any environment once the execution is tainted.
4. **Ledger intent** (see below).
5. **Resolve** the binding and the secret through `SecretStore`, and register the secret with the per-execution scrubber.
6. **Egress / SSRF guard:**
   - Resolve DNS once. Block private, loopback, link-local, CGNAT, IMDS `169.254.169.254`, ECS `169.254.170.2` and their IPv6 equivalents.
   - Connect to the **pinned IP**, with Host and SNI set.
   - Re-validate every redirect, with at most 3.
   - HTTPS on port 443 by default, plus a per-org domain allow-list.
   - AegisGrid's own internal hosts are never reachable.
7. **Call**, with timeout and retry set by the side-effect class.
8. **Post-process.** Enforce size and type limits, strip HTML, scrub secrets, and mark the output as **tainted** data.
9. **Record** the ledger result, the span and a redacted preview.

**Idempotency ledger** ([ADR-0010](../adr/0010-toolgateway-and-idempotency-ledger.md)):

- The key is `sha256(execution_id ‖ node_id ‖ call_index)`, unique per org.
- The intent row is inserted **before** the call.
- On re-entry, if the call is already `completed`, the recorded result is returned without calling again.
- If `args_hash` differs under the same key, the call is marked `unknown`.

What happens depends on the tool's side-effect class:

| Side-effect class | Retry behaviour |
|---|---|
| `read` | Retried freely. |
| `idempotent_write` | Retried with the same key. An `Idempotency-Key` header is sent where the API supports it. |
| `non_idempotent_write` with an ambiguous outcome | **Never auto-retried.** The call is marked `unknown`, the execution gets `needs_attention`, and an approval asks a human whether to retry. |

**DB tools.** MVP supports named, parameterized queries only. Recommended setup: a read-only role on the customer DB, plus `statement_timeout` and a row limit.

**MCP** ([ADR-0015](../adr/0015-mcp-remote-only-pinned.md)):

- **Remote only.** Streamable HTTP over TLS, through the egress guard. stdio MCP is **disallowed** until a sandbox exists.
- **Pinned at publish.** Running `tools/list` at publish pins hashes. Any drift at runtime **blocks** the call and raises an alert.
- **Locked down at runtime:**
  - Server-initiated sampling and elicitation are disabled.
  - Each server authenticates with its own CredentialReference. User tokens are never passed through.
  - All MCP output is tainted.

## Memory architecture

| Tier | Where | Lifetime |
|---|---|---|
| Execution state | LangGraph checkpoint (Postgres, `runtime` schema) | Execution plus 7 days, or until erasure |
| Hot transient | Redis | Ephemeral |
| Persistent agent memory | `memory_records` (Postgres) | Policy TTL, with an item cap per scope |

**Scope and ownership:**

- The scope key is `(organization_id, agent_id, environment_id, namespace, key)` plus an optional `subject_key`. The subject key defaults to the execution's.
- Memory **belongs to the Agent**, not the AgentVersion. It survives upgrades and rollbacks; rollback never rolls memory back.
- The version's memory policy decides what may be read and written.
- Production memory is never visible to dev.

**Provenance and trust:**

- Every record carries `source_execution_id`, `source_span_id`, `written_by_version_id`, `trust` (`trusted` or `untrusted`) and `expires_at`.
- Writes from tainted executions are stored as `untrusted` and excluded from default retrieval. This is the defence against memory poisoning.
- Retrieved memory is passed to the model as data.

**Retrieval:**

- Default retrieval uses exact key, namespace listing and tsvector full-text search.
- **pgvector is used only** when one scope holds more than a few thousand free-text items and full-text search demonstrably misses ([ADR-0019](../adr/0019-memory-ownership-and-pgvector.md)). In that case:
  - Store `embedding_model` with each vector.
  - The org, agent and environment filter goes **inside the SQL `WHERE`**.
  - `hnsw.iterative_scan` is set via `SET LOCAL`.
- **Version requirement (provisional):** pgvector ≥ 0.8.3, which fixes an HNSW vacuum corruption bug. To be verified against the exact pinned image and libraries.

**Deletion:**

- A sweeper job enforces TTLs and caps.
- Erasure is a hard `DELETE` by `subject_key`.

## Human approval architecture

**ApprovalRequest fields:**

- `org_id`, `execution_id`, `span_id`, `node_id`, `tool_call_id`, `agent_version_id`, `policy_ref`
- `requested_action` and `context`, both redacted previews. The context includes the reason: policy, taint, risk or unknown outcome.
- `action_hash`, `requested_by_principal`, `required_roles`
- `status`: `pending`, `approved`, `rejected` or `expired`
- `decided_by`, `decided_at`, `comment`, `consumed_at`, `expires_at` (default 24 h)

`action_hash = sha256(JCS{org, execution, version, node, tool_call_id, tool_name, tool_schema_hash, args, credential_ref_ids, env})`

**Deciding.** `POST …/approvals/{id}/decision {decision, action_hash, comment}` runs as one transaction:

1. `SELECT … FOR UPDATE` on the request.
2. Check it is still pending and not expired.
3. Re-check the approver's permission **now**, not as of when the request was created.
4. Enforce separation of duties.
5. Check the submitted hash equals the stored hash.
6. Update the request, write the audit event, and insert the resume job.

**Approvers cannot edit arguments.** To change them, reject the request; the workflow then creates a new one.

**Resuming.** The worker:

1. Loads the checkpoint and **recomputes** the hash.
2. Consumes the approval exactly once:
   ```
   UPDATE … SET consumed_at=now()
   WHERE status='approved' AND action_hash=? AND consumed_at IS NULL
   RETURNING …
   ```
3. Re-runs gateway policy: the binding is present, the credential is active, egress is allowed, and the version has not been revoked.

**Rejection and expiry:**

- A rejection resumes the run with `rejected`. The graph then takes its reject edge, or fails with `approval_rejected`.
- Expiry is handled by the reaper and is treated as a rejection.
- While waiting, an execution holds no worker and no lease.

**MVP limits** ([ADR-0014](../adr/0014-approvals-action-hash.md)):

- Single approver only. Two-approver approvals are deferred; they would need an `approval_decisions` child table.
- Approvals are for executions only. There are no deployment-request approvals.
