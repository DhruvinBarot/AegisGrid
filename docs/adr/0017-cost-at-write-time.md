# ADR-0017: Cost computed at write time from effective-dated prices

**Status:** Accepted 2026-10-26

## Context

Provider pricing changes. If costs are recomputed from today's prices, historical reports become wrong.

## Decision

### Prices

- Prices live in `model_prices`.
- Each row is immutable and effective-dated, with `effective_from` and `effective_to`.
- An exclusion constraint stops two rows for the same model from overlapping in time.
- A row can optionally be scoped to one organization, to override the global price for that org.
- A price change closes the current row and inserts a new one.

### Model calls

- Each `model_calls` row stores the token counts, the `model_price_id` used, and `cost` as a `Decimal` with its currency.
- `cost` is computed once, when the row is written, and never recomputed.
- Price corrections are made with compensating adjustment rows, never by updating the original row.
- If no price exists for the model, the row gets `cost = NULL` and is flagged `unpriced`. That cost can be filled in once, only while it is still `NULL`, and the fill is audited.

### Aggregation

- `usage_rollup_daily` is keyed by (org, day, env, agent, version, model).
- Workers batch-upsert it from in-memory totals roughly every 10 seconds.
- A nightly job reconciles it against `model_calls`.

## Consequences

- A price change never alters past reports.
- Budget checks before a call are approximate, using a Redis counter. Reconciliation afterwards is exact, from Postgres.

## Alternatives rejected

- **Recomputing cost from current prices:** this corrupts history.
- **Materialized views:** they need full refreshes and are awkward for near-real-time budget checks.
