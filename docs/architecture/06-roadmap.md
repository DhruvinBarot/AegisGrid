# 06 · Roadmap, First Vertical Slice, Open Decisions, Risks

## Phases

| Phase | Scope | Exit criteria |
|---|---|---|
| **0** ✅ | Repo, compose (pgvector, Redis), FastAPI `/healthz` and `/readyz`, async SQLAlchemy, Alembic (no revisions yet), pytest, ruff, pyright, Next.js shell | Done. Reconciled in [Phase 0 status](#phase-0-status) |
| **1** | Identity, orgs, memberships, invitations, 6-role RBAC, service accounts and API keys, environments (seeded), audit log, `OrgContext` + tenancy guard, DB roles and grants, psycopg 3 switch | Criteria met, awaiting human sign-off. See [Phase 1 status](#phase-1-status) |
| **2** | Agents, drafts, spec schema v1 + validation, publish (JCS hash, immutability trigger), deployments + rollback, protected deploy admin-only | Implemented 2026-11-02, awaiting sign-off. See [Phase 2 status](#phase-2-status) |
| **3** | Runtime: jobs table, claim + lease + **minimal reaper** (lease expiry → requeue), worker process, spec→LangGraph compile, Postgres checkpointer with **envelope-encrypted checkpoints**, `fake` (+ optional Ollama) provider, `llm` / `router` / `end` nodes, executions API (202 + `?wait=`), basic spans + model_calls, `subject_key` + nullable-content invariant | **First vertical slice passes**; checkpoint canary test passes |
| **4** | Integrations, bindings + revisions, CredentialReference + local SecretStore, ToolGateway (allow-list, schema validation, SSRF guard, scrubber, taint → **deny** for non-read tools), HTTP + DB tools, remote MCP with pinning, `tool` node + agent loop | SSRF suite; MCP drift blocked; secret never in span or checkpoint |
| **5** | Reliability: reaper backoff, dead jobs, fencing, deadlines, cancel, tool ledger + side-effect classes, retry/replay endpoints, idempotency keys | Kill a worker mid-run → no duplicate side effects; **benchmark gate** (10 exec/s, 500 concurrent, p95 claim < 1 s) |
| **6** | Full tracing: redaction pipeline, `payload_mode`, S3 offload (local: MinIO or filesystem), partitions, cost with `model_prices`, batched rollups, SSE live view, usage API, **erasure job** | Cost history unchanged after a price change; erasure test clears every location in [03](03-security-and-privacy.md#data-erasure) |
| **7a** | Approvals: gate node, gateway-forced and taint-forced approvals (replace deny), action hash, separation of duties, expiry, decide/resume | Approval replay and bait-and-switch tests rejected |
| **7b** | Memory: records, policy, provenance/trust, TTL sweeper, full-text retrieval | Untrusted memory excluded from default retrieval |
| **8** | Evaluations: datasets, evaluators, runs in eval mode, compare | Two versions compared on one dataset revision |
| **9** | Frontend MVP pages | Vertical slice usable from the UI |
| **9.5** | RLS enabled, CI runs under non-bypass roles, MFA for owners and approvers | Suite green under RLS |
| **10** | AWS: Terraform, ECS, RDS, ElastiCache, Secrets Manager + IAM split, KMS, S3, ALB/ACM, CI/CD, alarms, backups | Staging deploy; restore drill re-applies `erasure_requests` |

## First vertical slice

This slice is the **exit target of Phase 3**. It is not built first.

```
register/login → select org (membership) → create agent (draft: llm→end, output schema)
→ publish v1 (hash, immutable) → deploy v1 to "development"
→ POST /executions {agent, environment:"development", input} + Idempotency-Key → 202
→ worker claims job → resolves deployment→version → compiles graph (cached by hash)
→ fake/Ollama LLM call → structured output validated
→ spans (execution, node, llm) + model_call (tokens, latency, cost) + encrypted checkpoint
→ status succeeded → GET /executions/{id}?wait=… returns output + trace summary
```

Acceptance:

- A user from org B gets 404 on every org-A resource in the slice.
- A repeated idempotency key does not create a duplicate execution.
- An `agent_versions` UPDATE fails.
- After v2 is deployed, the old execution still shows v1 and its hash.
- If the worker is killed after the claim, the minimal reaper requeues the job and the execution completes once.
- The checkpoint canary string never appears in plaintext.

## Phase 0 status

Reconciled on 2026-10-26.

### Exists and works

**Backend**

- FastAPI app factory with `/healthz` and `/readyz`. Readiness is gated on Postgres only; Redis is reported but not gating.
- `Settings`:
  - `AEGIS_` prefix.
  - Fields `env`, `database_url` and `redis_url`.
  - `database_url` currently uses `postgresql+asyncpg`.
- Data and cache layer:
  - Async SQLAlchemy engine, `SessionLocal` and `Base`.
  - Ephemeral Redis client.
- Migrations: async Alembic `migrations/env.py` wired to `Base.metadata`. There are no revisions yet.
- Tooling:
  - pytest with 3 health tests that need compose services.
  - ruff and pyright.
  - `uv.lock`.
- Python 3.13-slim Dockerfile using uv.

**Frontend**

- Next.js 16, React 19 and TypeScript 7 shell (`layout.tsx`, `page.tsx`).
- pnpm lockfile and Dockerfile.

**Local stack and docs**

- Compose services:
  - `postgres` on host port 5433, using the floating image tag `pgvector/pgvector:pg17`.
  - `redis` 7.4.
  - `api` with reload.
  - `frontend`.
  - No worker yet; that is expected in Phase 3.
- README, `.env.example` and `.gitignore`.

The Phase 0 gaps (psycopg 3 switch, DB role bootstrap, `app/modules/`, CI workflow, pgvector pin) were all closed in Phase 1.

## Phase 1 status

Implemented and verified on 2026-10-30. Awaiting human sign-off.

### Exit criteria

| Criterion | Test |
|---|---|
| Cross-org 404 meta-test | `tests/integration/test_cross_org_404.py` |
| Refresh reuse | `tests/integration/test_refresh.py` |
| Last owner | `tests/integration/test_roles.py` |
| Role grants | `tests/integration/test_roles.py` |
| Audit immutability | `tests/integration/test_audit_immutability.py` |
| Tenancy guard raises | `tests/integration/test_tenant_guard.py`, `tests/unit/core/test_tenancy.py` |

The suite also passes from an empty Postgres volume, which runs the role bootstrap and every migration. That is the path CI takes. The CI workflow itself has not run yet, because nothing has been pushed.

### Security review

The foundation review (E1) and the final review (E2) both finished with their fixes applied. E2 fixes have regression tests in `tests/integration/test_e2_regressions.py`.

### Deferred from Phase 1

Known gaps, accepted for local development. Each must be closed in the phase shown.

| Gap | Risk | Close in |
|---|---|---|
| No `X-Forwarded-For` handling | Behind a load balancer every client shares one IP: logins are limited to 20 a minute **in total**, and audit IPs are wrong | Phase 10 |
| No request body size limit | Unauthenticated memory exhaustion. The ALB does not cap bodies | Phase 10 |
| Container runs as root with dev dependencies | Needs a production image | Phase 10 |
| JWT signing-key rotation not implemented | [ADR-0005](../adr/0005-identity-and-tokens.md) requires it every 90 days | Phase 10 |
| `redis_url` is a plain string | A password in the URL could reach logs | Phase 10 |
| Service-account role `approver` is allowed, and developers can grant it | A developer could hold approval rights through a machine identity, bypassing separation of duties. No approval permission exists yet | Phase 7a |
| No email delivery or verification | Invitations can't be delivered outside dev and test, and `/auth/register` reveals whether an account exists | Before the first external tenant |
| API keys outlive the member who created them | A departing member keeps any raw key they saved. Auto-revoking would break production automation, so keys stay live and offboarding must rotate them, using `created_by` to find them | Before the first external tenant |
| `api_keys.last_used_at` is never written | The API always returns null, so unused keys can't be spotted | Phase 6 |

## Phase 2 status

Implemented on 2026-11-02. Awaiting human sign-off. **Agents can't run yet:** execution is Phase 3.

### What exists

- **Module** `app/modules/agents/`: models, spec v1 ([ADR-0021](../adr/0021-agent-spec-schema-v1.md)), service, deployments, router.
- **Migration `0003`** (additive) adds four tables:
  - `agents`: the draft lives on the agent as `draft_spec` and `draft_rev`, per ADR-0007. There is no draft table.
  - `agent_versions`
  - `agent_deployments`
  - `agent_environment_state`: the active-deployment pointer.
- **Composite foreign keys enforce in the database:**
  - Org-owned rows reference only their own org.
  - A deployment's version belongs to the deployment's agent.
  - The state pointer references a deployment of the same agent and environment.
- **Immutability:**
  - `aegis_api` has only SELECT and INSERT on `agent_versions` and `agent_deployments`.
  - The `reject_immutable_mutation()` trigger blocks UPDATE, DELETE and TRUNCATE for every role, the table owner included.
  - Agents are never hard-deleted: there is no DELETE grant.
- **Permissions:** `agents:read` (every role), plus `agents:write`, `agents:publish` and `agents:deploy` (developer, admin, owner). Deploying to a protected environment also needs `environments:protected` (admin, owner). API keys get these only as scopes within their service account's role. An environment-restricted key can't see or deploy to other environments.
- **Concurrency:**
  - Publishing locks the agent row before computing `max(version_no)+1`. `UNIQUE(agent_id, version_no)` is the backstop.
  - Deploys lock the `(agent, environment)` pointer row. A racing first deploy gets `409 deployment_conflict`.
- **Audit actions:** `agent.create`, `agent.update`, `agent.draft_update`, `agent.publish`, `agent.deploy`, `agent.rollback`. Each is written in the same transaction as the change, and none carries spec content.
- **Dependencies added:** `rfc8785` (JCS), `jmespath`, `jsonschema`.

### Exit criteria

| Criterion | Test |
|---|---|
| Version UPDATE/DELETE rejected (API role and table owner) | `tests/integration/test_agent_db_guarantees.py` |
| Rollback creates a new deployment; history unchanged | `tests/integration/test_agent_deployments.py::test_deploy_rollback_history` |
| Hash stable across runs, key order and explicit defaults | `tests/unit/agents/test_spec.py` (golden hash), `tests/integration/test_agents.py` |
| Protected deploy admin-only | `test_agent_deployments.py::test_protected_environment_needs_admin` |
| Publishes serialize per agent | `test_agent_db_guarantees.py::test_publishes_of_one_agent_serialize_on_its_row_lock` |
| Cross-org 404 on every new route | `tests/integration/test_cross_org_404.py`, which discovers new routes automatically |

Tests now use Redis database 15, so they never touch the dev app's Redis keys.

### Known gaps

| Gap | Close in |
|---|---|
| No agent soft-delete endpoint (the column exists) | When the UI needs it |
| No version diff endpoint (`GET …/versions/{a}/diff/{b}` in [05](05-api-frontend-deploy.md)) | Phase 9 |
| No `Idempotency-Key` on create, publish or deploy (05 says creates accept it) | Phase 5 (idempotency keys) |
| `aegis_worker` has no grants on the agent tables yet | Phase 3 (the worker reads versions) |
| Provider names are validated, not checked against configured providers | Phase 3 (`ModelClient`) |

## Decisions still open

| Before | Decision |
|---|---|
| Phase 3 | Envelope-encryption library (AWS Encryption SDK vs Tink), verified for Python 3.13; dev model providers |
| Phase 4 | Default egress policy |
| Phase 6 | Retention / `payload_mode` defaults confirmed; local S3 (MinIO vs filesystem) |
| Phase 7a | Approval expiry default (proposed 24 h); self-approval default |
| Phase 10 | AWS region; RDS pgvector version (≥ 0.8.3, provisional); NAT vs endpoints; spend limits |
| Later | pgvector adoption, SQS, SSO/SCIM, per-org KMS, OTel export, analytics store |

## Hardest decisions to reverse

1. **Tenant keying:** `organization_id` on every row, composite FKs, UUIDv7 ids.
2. **Identity and token shape:** principals, separate identities, a JWT without org or roles.
3. **AgentVersion spec format and JCS content hash:** published versions last forever.
4. **Credentials by reference:** logical integration names, with the ToolGateway as the only egress.
5. **Execution data contract:** Postgres as the backbone, the checkpoint format and `thread_id` convention, and how idempotency keys are derived.

## Top risks

1. **Cross-tenant leak before RLS (Phases 1–9).** Mitigated by the guard, the meta-test as a CI gate and composite FKs.
2. **Indirect prompt injection leading to side effects or exfiltration.** Mitigated by the gateway, egress controls, taint deny/approval and MCP pins.
3. **Sensitive data outside the redaction boundary** (checkpoint primitives and metadata, logs, S3, backups). Mitigated by ADR-0012, the log deny-list, ADR-0018 and retention limits.
4. **Duplicate or ambiguous side effects under at-least-once delivery.** Mitigated by the ledger, per-call checkpointing, no auto-retry of ambiguous writes, and kill tests.
5. **Postgres as the single point of contention.** Mitigated by a write budget, batching, partitions, Multi-AZ, PITR plus restore drills, and the benchmark gate.

## Design Q&A

- **Why a modular monolith?** One team, one deploy, and atomic writes across tenant, version, execution and job. The executions seam keeps a later split cheap.
- **Where does `organization_id` live?** On every tenant-owned table. Global identity tables and prices are exempt, and checkpoints are a documented exception.
- **RLS?** Yes, at Phase 9.5, before the first external tenant. The roles and transaction-local settings exist from Phase 1.
- **What is immutable on publish?** The entire spec: prompts, model pin, workflow, tool schema copies and hashes, MCP pins, all policies, plus the hash and version number.
- **How is reproducibility guaranteed?** Executions are explainable and replayable through the version, binding revisions, runtime version, recorded spans and price ids. They are not bit-reproducible.
- **How are duplicate side effects avoided on retry?** Checkpoint resume, plus the tool ledger with deterministic keys, plus a human decision on ambiguous non-idempotent writes.
- **How is an approval made safe?** It is bound to an action hash, decided under a lock, consumed once on resume, and resumed only from the approval row.
- **Where does workflow state live?** In the encrypted LangGraph checkpoint in Postgres.
- **Redis or Postgres?** Postgres for everything durable. Redis for cache, rate limits, live pub/sub and nudges.
- **When is pgvector used?** Only when one scope holds more than a few thousand free-text items and full-text search misses.
- **How are MCP credentials isolated?** One reference per server, org and environment, read only by the worker, and resolved only by the gateway.
- **How are untrusted MCP servers handled?** Remote only, pinned, blocked on drift, no sampling, tainted output.
- **How do traces differ from logs?** Traces are tenant-scoped, structured product data. Logs are operator telemetry and never contain payloads.
- **How does historical cost stay accurate?** Prices are effective-dated and immutable, cost is computed at write time, and corrections are new rows.
- **How do environments relate to versions?** Versions are environment-agnostic. Each environment has its own deployment pointer, bindings, credentials, memory and budgets.
- **What is the minimum useful eval?** A revisioned dataset, deterministic evaluators, runs through the queue, and a two-version comparison.
- **What breaks first at 100×?** Span and checkpoint write volume, then jobs-table bloat, worker slots, connections, then filtered HNSW.
