# 04 · Observability, Cost, Latency, Evaluations

## Where data goes

| Destination | Contents | Never contains |
|---|---|---|
| `trace_spans` (Postgres, monthly partitions) | The span tree: execution → node → llm, tool, mcp, memory, approval, retry. Each span carries status, timings, attempt, bounded attributes, redacted previews, payload refs and error | Secrets |
| `execution_events` | Lifecycle transitions, used by the timeline and SSE | Payloads |
| `model_calls` | Tokens, latency, time to first token (ttft), price id, cost | Prompt text (that lives in span payloads) |
| S3 | Payloads over 64 KB, gzipped, stored at `orgs/{org}/executions/{exec}/{span}.json.gz` with their sha256 and KMS encryption. Locally: MinIO or the filesystem (decided in Phase 6) | — |
| App logs (structured JSON → CloudWatch) | Operational events, errors, request_id, org_id, execution_id, trace_id | Payloads, request bodies, prompts |
| Metrics (CloudWatch EMF or Prometheus) | Queue depth and age, claim latency, lease expiries, execution duration, errors by class, tokens, cost | High-cardinality labels |

**Span fields:**
- Identity: `id`, `organization_id`, `execution_id`, `trace_id`, `span_id`, `parent_span_id`.
- Type: `kind`, `name`.
- Outcome: `status`, `started_at`, `ended_at`, `duration_ms`, `attempt`, `seq`, `error`.
- Content: `attributes` (JSONB, bounded size), `input_ref`, `output_ref`.

The field names are OTel-shaped so an exporter can be added later. Postgres stays the source of truth because OTel backends sample and drop data.

**Live view:**
- The worker publishes a "span updated" message to Redis on the channel `trace:{org}:{execution}`.
- The SSE endpoint authorizes the reader first, then builds the channel name on the server.
- On reconnect, the client sends `Last-Event-ID` = `seq` and missed events are replayed from Postgres.

**Retention:**

| Data | Retention |
|---|---|
| Content payloads | 30 days hot |
| Span metadata | 1 year |
| Rollups | Indefinite |
| Audit log | 1 year by default, up to 7 years |

Old data is removed by detaching and dropping partitions. S3 lifecycle rules move payloads to Infrequent Access and then Glacier, or delete them.

## Cost and latency ([ADR-0017](../adr/0017-cost-at-write-time.md))

**`model_calls` columns:**
- Context: `organization_id`, `execution_id`, `span_id`, `agent_version_id`, `environment_id`, `provider`, `model_id`.
- Tokens: `input_tokens`, `output_tokens`, `cached_input_tokens`, `reasoning_tokens`.
- Timing and status: `latency_ms`, `ttft_ms`, `status`.
- Price: `model_price_id`, `cost`, `currency`, `created_at`.

**How cost is recorded:**
- Prices live in `model_prices` rows. Each row is effective-dated and immutable; a new price closes the previous row, and an exclusion constraint prevents overlaps.
- Cost is computed **at write time** and stored as `Decimal` together with the `model_price_id`. History is never recomputed.
- A price correction is a compensating adjustment row, never an UPDATE.
- If no price row exists, the call is stored with `cost = NULL` and flagged `unpriced`. It can be filled in once, **only from NULL**, and the fill is audited.
- Failed or partial calls record whatever usage the provider reported.

**Aggregation:**
- `usage_rollup_daily` is keyed by (org, day, env, agent, version, model).
- Each worker accumulates totals in memory and flushes about every 10 seconds with `INSERT … ON CONFLICT DO UPDATE`. This avoids hot-row contention.
- A nightly job reconciles the rollup against `model_calls`.

**Budgets:**
- Before each call, a cheap approximate check runs against a Redis counter.
- After the call, the exact figure is reconciled from Postgres.

**Latency:** stored on spans (all kinds) and on `model_calls` (`latency_ms`, `ttft_ms`). p50 and p95 are computed per agent and version from spans.

## Evaluations

The minimum useful system ([ADR-0016](../adr/0016-traces-and-evals-minimal.md)):

- `evaluation_datasets(id, org, name, revision)` holds `evaluation_cases(dataset_id, input, expected, tags[], position)`.
- `evaluators(id, org, kind, config)`, where `kind` is one of `exact`, `contains`, `regex` or `json_schema`.
- `evaluation_runs(id, org, dataset_id, dataset_revision, agent_version_id, environment_id, evaluator_ids[], status, summary)`. The summary holds `{pass_rate, mean_score, cost, p50/p95 latency}`.
- `evaluation_results(run_id, case_id, execution_id, score, passed, evaluator_outputs, error)`.

**How a run executes:**
- The run enqueues one `kind='eval'` execution per case through the **normal queue**, so tracing and cost come for free.
- The gateway runs in **eval mode**: non-read tools return the version's declared mocks or are blocked.
- To compare two versions, run both against the same dataset revision and join the results on case.

**Planned extensions that need no schema upheaval:**
- **Promotion gate:** deploying to a protected environment requires the version's latest eval to meet its threshold.
- **Regression:** an eval runs automatically on publish.
- **Production sampling:** a percentage of production executions are copied in as redacted cases, keeping `subject_key` so they remain erasable.
- **Human ratings:** a `ratings` table keyed to an execution or a result.
- **LLM-as-judge:** an evaluator kind that writes and costs its own `model_calls`.
