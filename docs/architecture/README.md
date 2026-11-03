# AegisGrid Architecture

Multi-tenant control plane for enterprise AI agents.

- **Status:** approved with changes, 2026-10-26 (architecture v2, plus the validation review, plus revisions from the human).
- **Decision records:** [`../adr/`](../adr/README.md).
- **Change control:** if this document and an ADR disagree, the ADR wins. Changing an accepted decision requires a new ADR that supersedes the old one.

| Doc | Contents |
|---|---|
| this file | Summary, scope, high-level architecture, control vs execution plane, repository structure |
| [01-tenancy-and-data.md](01-tenancy-and-data.md) | Domain model, multi-tenancy, RBAC, database design, AgentVersion model |
| [02-execution-runtime.md](02-execution-runtime.md) | Execution lifecycle, async queue, LangGraph runtime, tools and MCP, memory, approvals |
| [03-security-and-privacy.md](03-security-and-privacy.md) | AuthN, AuthZ, credentials, threat model, checkpoint protection, data erasure |
| [04-observability-and-evals.md](04-observability-and-evals.md) | Traces, logs, metrics, cost and latency, evaluations |
| [05-api-frontend-deploy.md](05-api-frontend-deploy.md) | API design, frontend, AWS deployment |
| [06-roadmap.md](06-roadmap.md) | Phases, first vertical slice, open decisions, risks, design Q&A |

## 1. Executive summary

AegisGrid is a **modular monolith**: one Python codebase that runs as two processes.

- `api` is the control plane and the HTTP edge.
- `worker` is the execution plane.

The two processes communicate **only through PostgreSQL**. Postgres is the source of truth for everything:

- tenants and agent versions
- the job queue (`FOR UPDATE SKIP LOCKED` plus leases)
- LangGraph checkpoints
- approvals, traces, usage and audit

Redis is ephemeral only: cache, rate limits, pub/sub for live traces, and cancel nudges. Flushing Redis loses nothing.

Load-bearing ideas:

1. **Structural tenant isolation.**
   - Every tenant row carries `organization_id`, with composite FKs `(organization_id, id)`.
   - An ORM guard raises if a query runs without an org context.
   - The tenant comes from the URL org plus a live membership lookup. It never comes from the token or the request body.
   - Postgres RLS is enabled before the first external tenant.
2. **Immutable, content-hashed AgentVersions.**
   - Environments decide *which* version is live, through append-only deployments.
   - Environments decide *where* integrations point, through bindings.
   - Credentials are only ever referenced, never copied into a version.
3. **A single ToolGateway** is the only path to the outside world. It enforces:
   - allow-lists and MCP pins
   - SSRF/egress rules
   - secret resolution
   - approvals and taint
   - the idempotency ledger
   - trace emission

   The LLM is treated as an untrusted planner.
4. **Async execution from the first runtime phase.**
   - `202 Accepted`, or `?wait=` for a sync-style call.
   - Delivery is at-least-once, made safe by checkpoint resume and a tool-call intent/result ledger.
   - Executions paused for approval hold no worker.
5. **Explainable executions.**
   - Each execution records its exact version, environment, deployment, binding revisions and runtime version.
   - Traces are stored as structured spans.
   - Cost is computed at write time against effective-dated prices.

## 2. Product scope (long term)

- Identity and tenancy: orgs/workspaces, users, invitations, RBAC, service accounts and API keys, SSO/SCIM.
- Agents: versions, environments, promotion and rollback.
- Runtime: declarative LangGraph workflows, and tools (HTTP, DB, internal, MCP; browser/computer-use later).
- Execution: model providers, sync and async runs, persistent memory, human approvals.
- Visibility: execution history, traces, replay, cost/token/latency analytics, budgets.
- Quality: evaluations and gated promotion.
- Governance: credential management, audit logs, production monitoring and alerting.

## 3. MVP scope

| Area | MVP |
|---|---|
| Identity | Email + password (argon2id), access JWT (ES256, 10 min), rotating refresh cookie, server-side sessions, service accounts with API keys |
| Tenancy | Orgs, memberships with **6 roles**, invitations, composite FKs, ORM tenant guard, cross-org 404 meta-test, DB roles split from day one |
| Agents | Agent with a single draft, publish an immutable AgentVersion, deploy per environment, rollback |
| Environments | Seeded `development`, `staging`, `production` (protected). Custom environments allowed |
| Runtime | Declarative spec. Node types: `llm`, `tool`, `router`, `approval_gate`, `memory_read`, `memory_write`, `end`. The agentic loop compiles to `llm`↔`tool`. Structured output |
| Models | Own thin `ModelClient`. `fake` (deterministic, free), Ollama (free, optional), hosted providers (PAID, optional) |
| Tools | HTTP/REST, named parameterized DB queries, built-ins, **remote** MCP with schema/description pinning |
| Execution | Postgres queue, leases, reaper, retries, cancel, timeouts, idempotency keys, `?wait=` |
| Approvals | Gate node, plus gateway-forced approvals (policy/taint). Action-hash binding, separation of duties, expiry. **Single approver** |
| Memory | Key/namespace records with full-text search. pgvector optional per policy |
| Observability | Spans, lifecycle events, model calls, daily usage rollups, redaction, SSE live view |
| Evals | Datasets, deterministic evaluators, runs, version comparison |
| Audit | Append-only audit log, written in the same transaction as the change |
| Frontend | Pages listed in [05](05-api-frontend-deploy.md#frontend) |
| Deploy | ECS Fargate, RDS, ElastiCache, Secrets Manager, KMS, S3, ALB (PAID, Phase 10) |

## 4. Explicitly deferred

- **Identity and access:**
  - SSO (OIDC/SAML) and SCIM.
  - MFA (lands at Phase 9.5 for owners and approvers).
  - Email delivery and email verification.
- **Approvals and deploys:**
  - Two-approver approvals.
  - Deployment-request workflow. Protected deploys are admin-only in the MVP.
- **Tools and MCP:**
  - stdio MCP (waits for a sandbox).
  - Browser and computer-use tools.
  - Free-form SQL tools.
- **Runtime and evals:**
  - Subgraphs.
  - Resume-from-failed-checkpoint.
  - LLM-as-judge, human ratings, production sampling, enforced promotion gates.
- **Keys and crypto-shredding:**
  - Per-org KMS keys and crypto-shredding.
  - KMS-backed JWT signing (lands at Phase 10).
- **Platform:**
  - Egress proxy, SQS, read replicas, OTel export, analytics store.
  - Multi-region, Kubernetes, microservices, Kafka.
  - Dedicated vector DB.
- **Product:**
  - Visual graph editor.
  - Scheduled and webhook triggers (designed, not built).
  - Billing.

## 5. High-level architecture

```
            Browser (Next.js)              Machine callers (API keys)
                   │  HTTPS                        │
                   ▼                               ▼
        ┌──────────────────────── ALB ─────────────────────────┐
        ▼                                                      │
 ┌──────────────── api process (FastAPI) ─────────────────┐    │
 │ identity · organizations · environments · agents       │    │
 │ integrations · approvals(decide) · evaluations · audit │    │
 │ executions (create/read/cancel) · observability(read)  │    │
 └───────┬──────────────────────────────┬─────────────────┘    │
         │ INSERT execution+job (1 tx)  │ SSE ← Redis pub/sub
         ▼                              │
 ┌────────────── PostgreSQL 17 (+pgvector) ───────────────┐
 │ tenants · versions · jobs · checkpoints (runtime schema)│
 │ executions · spans · model_calls · approvals · memory   │
 │ audit_log · usage_rollup_daily                          │
 └───────▲───────────────────────────────▲────────────────┘
         │ claim (SKIP LOCKED), lease,   │ checkpoints, spans
 ┌───────┴───── worker process ──────────┴────────────────┐
 │ JobQueue → Runtime (spec→LangGraph) → ModelClient       │
 │                     └→ ToolGateway → HTTP / DB / MCP    │
 │                          ├ SecretStore (Secrets Mgr)    │
 │                          ├ egress/SSRF guard            │
 │                          └ approval/taint/ledger        │
 └────────────────────────────────────────────────────────┘
   Redis: cache, rate limits, live-trace pub/sub, cancel nudge (ephemeral)
   S3: payloads > 64 KB, exports · KMS: envelope keys
```

## 6. Control plane vs execution plane

| | Control plane (`api`) | Execution plane (`worker`) |
|---|---|---|
| Owns | Users, orgs, memberships, RBAC, agents/drafts/versions/deployments, environments, integrations/bindings, credential **metadata**, policies, datasets, approval **decisions**, audit, dashboards | Job claiming, spec compilation, LangGraph runs, model calls, ToolGateway, secret **resolution**, retries/timeouts/cancel, checkpoints, memory ops, spans, model_calls |
| Writes | Control tables, `executions` (insert, cancel flag), `jobs` (insert), approval decision + resume job | Execution status, spans, events, model_calls, tool-call ledger, checkpoints, memory |
| DB role | `aegis_api` | `aegis_worker` (+ `aegis_system` for claim/reaper only) |
| Secrets | **Write-only** (IAM denies `GetSecretValue`) | **Read-only** |
| Inbound network | ALB | **None** |

**The seam between the planes** is the `executions` module. It holds the state machine, the `JobQueue` protocol and the execution/job tables, and both planes import it.

**Import rules**, enforced by an import-boundary test:

- Control-plane modules never import `runtime`, `gateway` or `memory`.
- The runtime reads control-plane data only through narrow read services, such as `get_version_spec` and `resolve_bindings`.

## 7. Repository structure

```
AegisGrid/
├── backend/
│   ├── app/
│   │   ├── main.py              # api entry (exists)
│   │   ├── worker.py            # worker entry (Phase 3)
│   │   ├── core/                # config, db, cache (exist); ids, errors, tenancy guard, security
│   │   └── modules/
│   │       ├── identity/        # users, identities, principals, sessions, tokens, service accounts, api keys
│   │       ├── organizations/   # orgs, memberships, invitations, rbac
│   │       ├── environments/
│   │       ├── audit/
│   │       ├── agents/          # agents, drafts, versions, deployments, spec schema
│   │       ├── integrations/    # integrations, bindings, credential refs, tool defs, MCP discovery
│   │       ├── executions/      # SEAM: records, state machine, JobQueue
│   │       ├── runtime/         # spec compiler, LangGraph runner, model clients, worker loop, reaper
│   │       ├── gateway/         # ToolGateway: policy, egress, secret store, ledger, MCP client
│   │       ├── approvals/
│   │       ├── memory/
│   │       ├── observability/   # spans, events, model_calls, rollups, redaction, SSE
│   │       └── evaluations/
│   │       # each module: router.py · schemas.py · models.py · service.py · repository.py
│   ├── migrations/              # Alembic (exists, no revisions yet)
│   └── tests/                   # unit · integration · tenancy meta-tests
├── frontend/src/app/[org]/…     # pages (Phase 9); src/lib/api = generated OpenAPI client
├── infra/docker/ (exists) · infra/db/ (role bootstrap, Phase 1) · infra/terraform/ (Phase 10)
└── docs/architecture/ · docs/adr/
```

Cross-module calls go through the target module's `service.py`, never its repository or models.
