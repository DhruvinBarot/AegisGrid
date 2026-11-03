# ADR-0018: Data erasure via `subject_key` and nullable content

**Status:** Accepted 2026-10-26

## Context

Customer data can end up in many places: execution inputs and outputs, span previews, S3 payloads, checkpoints, the tool ledger, approval previews, memory, logs, backups, and third-party providers. An erasure request has to reach all of these, and must be completed within regulatory deadlines.

## Decision

1. **`subject_key`.** An optional field, set when an execution is created, holding the caller's pseudonymous id for the data subject.
   - It propagates to memory writes and to S3 object tags.
   - Without it, erasure works per execution or through retention.
   - The API documentation states this clearly.
2. **Invariant: content is nullable and erasable; metadata is not.**
   - Erasing an execution sets its content columns to NULL and sets `erased_at`.
   - IDs, timings, token counts, cost and status are kept for billing and audit.
3. **Erasure job.** `POST /orgs/{org}/data-subjects/{key}/erase` (owner or admin) enqueues a job in the worker. It:
   - nulls execution content, span content, ledger result refs and approval previews,
   - deletes the S3 prefixes for the affected executions,
   - deletes checkpoints by `thread_id`,
   - hard-deletes `memory_records`,
   - records the request in `erasure_requests` and writes an audit event containing counts only.
4. **Supporting rules.**
   - The S3 payload bucket has versioning off, or noncurrent versions expire after 1 day.
   - The log deny-list and its test keep payloads out of application logs.
   - CloudWatch logs are retained for 30 days.
   - After any database restore, the `erasure_requests` ledger is re-applied, and backups age out within the PITR window.
5. **Org deletion.** Hard-deletes the org's tenant rows, its S3 prefix, its secrets path and its checkpoints. Per-org KMS crypto-shredding is deferred.
6. **Timing.**
   - Phase 3: the `subject_key` column, the nullable-content invariant and the S3 key layout.
   - Phase 6: the erasure job.
   - The 30-day content retention is the backstop.

## Consequences

- Erasure by subject works whenever callers supply `subject_key`.
- Content shared with LLM providers is outside our control. The mitigations are zero-retention settings and DPAs, and this is documented.

## Alternatives rejected

- **Full-text search across payloads to find a subject's data:** impractical and unreliable.
- **Crypto-shredding only:** needs per-subject keys, which is too costly and complex for the MVP.
