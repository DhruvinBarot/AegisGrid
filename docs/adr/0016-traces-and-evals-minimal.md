# ADR-0016: Span-based traces with write-time redaction; minimal evaluations

**Status:** Accepted, 2026-10-26

## Context

Every production execution must be understandable after the fact. Trace payloads contain sensitive data. The evaluation system has to be useful without becoming its own ML platform.

## Decision

### Traces

- **Tables:**
  - `trace_spans` holds the span tree. Spans use OTel-shaped ids and are partitioned monthly.
  - `execution_events` holds a small lifecycle log.
- **Payload storage:**
  - Payloads over 64 KB go to S3. The span keeps a pointer plus a sha256 of the payload.
  - Spans themselves keep only bounded attributes and redacted previews.
- **Redaction:**
  - It runs **in the worker before both the Postgres insert and the Redis publish**: first the secret scrubber, then field masking driven by the schema.
  - `payload_mode` (`full`, `redacted` or `metadata_only`) is set per org and per environment. Defaults are `redacted` for protected environments and `full` for dev.
- **Logs:**
  - Application logs never contain payloads. A deny-list processor plus a test enforce this.
  - Logs are operator telemetry. Traces are tenant-facing product data.
- **Retention:** content 30 days, metadata 1 year, rollups indefinitely.

### Evaluations

- **Data model:**
  - A dataset has a `revision` and contains cases.
  - Evaluators are one of `exact`, `contains`, `regex` or `json_schema`.
  - A run binds `(dataset_revision, agent_version)` and produces per-case results.
- **Execution:**
  - Each case runs as a `kind='eval'` execution through the normal queue.
  - The gateway runs in eval mode: tools that aren't read-only are mocked or blocked.
- **Comparison:** two runs over the same dataset revision, joined on case.

## Consequences

- Traces are queryable per tenant, and their retention is bounded.
- Eval reuses the runtime, so its cost and trace data come for free.
- LLM-as-judge, human ratings, sampling and promotion gates can be added without changing the schema.

## Alternatives rejected

- **Logs only:** not queryable, and they leak sensitive data.
- **Full event sourcing:** too much write volume and complexity for MVP.
- **An external OTel backend as the source of truth:** these backends sample and drop data.
