# 01 · Tenancy, RBAC, Data Model, AgentVersions

## Domain model

Identity and tenancy:

- **Global tables:** `User`, `UserIdentity`, `Session`, `RefreshToken`, `SigningKey`.
- **`Organization`** is the tenant root.
- **Org-scoped identity:**
  - `Principal` (a user or a service account)
  - `OrganizationMembership` (holds the role)
  - `Invitation`
  - `ServiceAccount` and `ApiKey`

Configuration (control plane):

- **Environment.** Each environment holds an `IntegrationBinding` per integration, with an append-only revision history and a `CredentialReference`.
- **Agent.** An `Agent` is the stable identity and holds one draft. Its versions are `AgentVersion` rows, immutable, each with a spec and a `content_hash`.
- **Deployments.** `AgentDeployment` rows are append-only. `agent_environment_state` points each environment at its active version.
- **Integrations.** An `Integration` has a kind of `http`, `db`, `builtin` or `mcp`. Its `ToolDefinition`s form a mutable catalog that versions pin.
- **Models.** `ModelProvider` holds a credential by reference. `ModelPrice` is global and effective-dated, with an optional per-org override.

Runtime records (execution plane):

- **Execution** is the mutable spine. It links to its version, environment and deployment, and records which binding revisions it used. It has:
  - `Job` rows
  - `TraceSpan` rows (mostly append-only)
  - `ExecutionEvent` rows (lifecycle)
  - `ModelCall` rows (append-only)
  - `ToolCall` ledger rows (intent, then result)
  - `ApprovalRequest` rows
- **MemoryRecord** belongs to an Agent and is partitioned by environment.

Evaluation and audit:

- **Evaluations.** An `EvaluationDataset` is revisioned and holds `EvaluationCase`s. `Evaluator`s score cases. An `EvaluationRun` runs a dataset against a version and produces `EvaluationResult`s, each tied to an Execution.
- **AuditEvent** is append-only.

Consolidations, so these do not get their own table:

| Concept | Where it lives |
|---|---|
| MCPServer | An `Integration` with `kind='mcp'` |
| ToolBinding | Inside the version spec |
| UsageRecord | `ModelCall` |
| IdempotencyKey | A unique column on `executions` (v1) |
| AgentDraft | Columns on `Agent` |

## Multi-tenant model

### Membership and roles

A `User` has no role of its own. A `Principal` (a user or a service account) holds an `OrganizationMembership(organization_id, principal_id, role)`, with one role per membership. The role → permission map lives in versioned code, not in the database.

**The six roles:**

- **owner** and **admin** run the org.
- **developer** builds and tests in non-protected environments.
- **operator** runs production; it is the normal role for service accounts. ([ADR-0006](../adr/0006-rbac-six-roles.md))
- **approver** decides approvals and nothing else.
- **viewer** has read access.

| Permission | owner | admin | developer | operator | approver | viewer |
|---|---|---|---|---|---|---|
| Org settings / delete org | W / delete | W | – | R | – | R |
| Members, invites, roles | any role | ≤ admin | R | R | R | R |
| Service accounts / API keys | W | W | own, non-protected envs | – | – | – |
| Agent drafts, publish version | ✓ | ✓ | ✓ | R | R | R |
| Deploy to non-protected env | ✓ | ✓ | ✓ | – | – | – |
| Deploy to protected env | ✓ | ✓ | – | – | – | – |
| Create executions | ✓ | ✓ | non-protected only | ✓ any env | – | – |
| Read / cancel executions | RW | RW | RW | RW | R | R |
| Decide approvals | ✓ | ✓ | if policy names role | if policy names role | ✓ | – |
| Integrations / credentials | W | W | use in dev, read metadata | read metadata | – | – |
| Evaluations | W | W | W | R | R | R |
| Audit log | R | R | – | – | – | – |

Rules that always apply:

- You can grant only roles at or below your own. Only an owner can grant owner.
- Nobody can change their own role.
- The last owner cannot be removed or demoted. This is checked with `SELECT … FOR UPDATE` across the org's owner memberships.
- Invites:
  - The token is 256 bits, stored hashed, single use, and expires after 7 days.
  - Each invite is bound to an email address, and the role is fixed when the invite is created.
  - When the invite is accepted, the system re-checks that the inviter is still allowed to grant that role.
  - An inviter's pending invites are revoked when the inviter is removed.
- API keys cannot manage members, keys or credentials, and cannot exceed the permissions of the principal that created them.
- Separation of duties: whoever starts an execution cannot approve its actions. Single-owner orgs may enable `allow_self_approval`, but only for non-protected environments, and every such decision is flagged in the audit log.

### Request path

1. `Depends(require("perm"))` reads the URL `org_id` and the JWT `sub`/`sid`.
2. One query joins the membership with a non-revoked session.
3. The result is a frozen `OrgContext`.
4. No membership returns **404**. A member without the permission gets **403**.

### Structural guards

- Repositories take an `OrgContext`, and the base repository adds `organization_id = ctx.org_id` to every query.
- A `do_orm_execute` hook uses `with_loader_criteria(TenantMixin, …)` and **raises** if a tenant model is queried without org context. The only escape is an explicit `system_scope()`, used by the job claimer, the reaper and migrations tooling.
- `session.get()` is banned on tenant models, enforced by a lint rule or a test.
- **Cross-org meta-test** (a mandatory CI gate):
  1. Enumerate every `/orgs/{org_id}/…` route.
  2. Seed orgs A and B.
  3. Call every route as a user of A, using B's resource ids.
  4. Assert 404.
- Redis keys follow `ag:{org_id}:{purpose}:…`. The SSE channel name is built on the server after authorization and is never supplied by the client.

## Database design

### Conventions

- **IDs:** UUIDv7 primary keys, generated in the app. The API exposes them with a type prefix, e.g. `exe_…`.
- **Time:** `timestamptz` everywhere. `updated_at` only on mutable tables.
- **Money:** `numeric(20,10)` plus a `currency` column. Python `Decimal`, never float.
- **JSONB vs columns:** a value becomes a column if anything filters, joins or constrains on it. Specs, snapshots and attributes are JSONB, each carrying a `schema_version`.
- **Soft delete** (`deleted_at`) applies only to Agent, Environment, Integration and Dataset, because history must keep referencing them.
- **History tables** are never soft-deleted. They are purged only by retention policy.
- **Memory** is hard-deleted.
- **Tenant tables:**
  - `organization_id NOT NULL` and `UNIQUE(organization_id, id)`.
  - Child tables use composite FKs, so a cross-tenant reference cannot be represented.
  - Every tenant index starts with `organization_id`.

### Entities

| Entity | Key fields | Immutability | Scope |
|---|---|---|---|
| users, user_identities, sessions, refresh_tokens, signing_keys | `user_identities(provider, issuer, subject)`; refresh tokens store `sha256`, `family_id`, `used_at` | Mutable. Users are pseudonymized, not deleted, while they appear in the audit log | Global |
| organizations | slug, name, settings | Mutable | Root |
| principals, memberships, invitations, service_accounts, api_keys | role; invite `token_hash`; API key `key_id` + `HMAC(pepper, secret)`, scopes, env, `expires_at` | Mutable, every change audited | Org |
| environments | name, kind, `protected`, settings (rate limits, budgets, `payload_mode`) | Mutable | Org |
| agents | name, slug, `draft_spec`, `draft_rev` (optimistic lock) | Draft is mutable | Org |
| agent_versions | agent_id, version_no, `spec`, `spec_schema_version`, `content_hash`, created_by, notes | **Immutable** (a trigger rejects UPDATE/DELETE) | Org |
| agent_deployments + agent_environment_state | env, agent, version, deployed_by, reason (deploy/rollback), supersedes_id; state → active_deployment_id | Deployments **append-only**; the state row is the single atomic pointer | Org |
| integrations | kind, logical_name, display_name | Mutable | Org |
| integration_bindings + _revisions | (integration, env) → config, credential_ref_id, config_version | Each change writes an **append-only** revision | Org + env |
| credential_references | env, name, kind, provider, `secret_path`, status, rotated_at, expires_at | Metadata only, **never a secret value** | Org + env |
| tool_definitions | integration, name, schemas, `side_effect_class`, `risk`, `schema_hash`, `description_hash` | Mutable catalog; versions pin copies | Org |
| model_providers / model_prices | prices: per-Mtok rates for input, output and cached input, currency, `effective_from/to`, nullable org override | Prices **immutable**; an exclusion constraint prevents overlapping ranges | Global / Org |
| executions | see [02](02-execution-runtime.md#execution-snapshot) | Mutable spine; terminal states frozen by trigger | Org |
| jobs | queue (see 02) | Mutable; finished rows pruned | Org |
| trace_spans | span tree, status, timings, bounded attributes, payload refs | Mostly append-only; **monthly partitions** | Org |
| execution_events | lifecycle transitions | Append-only | Org |
| tool_calls | key, `args_hash`, status (intent/completed/failed/unknown), result_ref | Intent → final, then frozen | Org |
| model_calls | provider, model, token counts, latency, `model_price_id`, cost | **Append-only**; monthly partitions | Org |
| usage_rollup_daily | (org, day, env, agent, version, model) → totals | Batch-upserted, reconciled nightly | Org |
| approval_requests | see 02 | Decided once, consumed once | Org |
| memory_records | see 02 | Hard delete | Org + env |
| evaluation_* | see 04 | Results append-only | Org |
| audit_log | actor, action, target, ip, request_id, outcome, redacted metadata | **Append-only** (INSERT/SELECT grants + trigger); monthly partitions | Org (nullable for pre-org auth events) |
| erasure_requests | org, `subject_key_hash`, requested_at, completed_at | Append-only; re-applied after any restore | Org |

### Indexes that matter

- `executions`:
  - `(org, agent, created_at desc)`
  - a partial index on active statuses
  - unique `(org, created_by_principal, idempotency_key)`
- `jobs`:
  - `(run_after) WHERE state='queued'`
  - `(lease_expires_at) WHERE state='running'`
  - unique `(execution_id) WHERE state IN ('queued','running')`
- `trace_spans(org, execution_id, started_at)`
- `approval_requests(org, status, expires_at) WHERE pending`
- `memory_records`: a unique scope key, plus GIN on a tsvector, plus a partial HNSW index once pgvector is adopted.

### Database roles ([ADR-0004](../adr/0004-database-roles.md))

| Role | Use | RLS |
|---|---|---|
| `aegis_migrator` | Owns tables; runs Alembic | n/a (owner) |
| `aegis_api` | The api process. No access to the `runtime` schema. No INSERT on spans, events, model_calls or tool_calls | Subject to RLS |
| `aegis_worker` | The worker process. Only role that can access the `runtime` (checkpoint) schema | Subject to RLS |
| `aegis_system` | Job claimer and reaper only, through a separate engine | BYPASSRLS |

The roles and grants exist from Phase 1. Enabling RLS later (Phase 9.5) is then just a migration:

- Policies: `ENABLE` + `FORCE ROW LEVEL SECURITY` with `organization_id = current_setting('app.org_id', true)::uuid`. An unset setting yields NULL, which matches no rows.
- Session setup: an `after_begin` hook runs `set_config('app.org_id', :org, true)`. It is transaction-local, so it is safe with pools and with PgBouncer in transaction mode.
- CI runs the whole suite under the non-bypass roles.

### Documented exception: LangGraph checkpoint tables

These tables are owned by the library and have no `organization_id`. They are protected another way:

- They live in a `runtime` schema that only the worker role can access.
- Content is encrypted ([ADR-0012](../adr/0012-checkpoint-protection.md)).
- `thread_id = execution_id`.
- They are never exposed through the API.
- They are deleted 7 days after the execution reaches a terminal state, or immediately on erasure.

## AgentVersion model

| Concept | Meaning |
|---|---|
| Draft | `agents.draft_spec`. Mutable, with an optimistic lock (`draft_rev`). One draft per agent |
| Publish | `POST …/versions`: validate → compile → check tool schemas and live MCP hashes → canonicalize (RFC 8785 JCS) → `content_hash` → insert an immutable row with `version_no = n+1` |
| Active version | Per environment: the latest `AgentDeployment`. A published version is live nowhere until it is deployed |
| Rollback | A **new** deployment row (`reason='rollback'`) pointing at an older version. Versions are never mutated or deleted |

### Snapshotted into the spec

Everything below is copied in full; nothing points at a mutable row.

1. The system prompt and templates, with declared variables.
2. Model config: provider, a **pinned model id** (no "latest" aliases), parameters and the fallback chain.
3. The workflow spec and `spec_schema_version`.
4. Tool bindings:
   - the logical integration and tool name
   - a **copy** of the input and output schemas, plus `schema_hash`
   - side-effect class and risk
   - per-tool approval, timeout and retry overrides
   - eval-mode mocks
5. MCP references: the logical integration, allowed tool names, and **pinned hashes of each tool's name, schema and description**.
6. Memory policy: namespaces read and written, TTL, max items, recall method, subject scoping.
7. Approval policy: which tools, nodes and risk levels require approval; approver roles; expiry; self-approval rule.
8. Retry and timeout policy: per node, plus wall clock, max steps and max cost.
9. Output JSON Schema and the eval config.

### Kept as references, never snapshotted

- **Credentials.** They rotate, and snapshotting would freeze leaked secrets into history.
- **Per-environment endpoints.** The same version artifact must run in dev, staging and prod.
- **Prices, rate limits and budgets.**

The spec names integrations by **logical name**, for example `crm`. When an execution starts, that name resolves to the environment's binding revision, and the revision ids are recorded on the Execution.

**Rollback does not undo external side effects.** Emails, tickets and writes from earlier executions remain. Rollback only changes which version *future* executions use. The UI says so on the rollback confirmation. Compensating actions are separate workflows.

### Promotion

Promotion means deploying the same immutable version to the next environment, for example dev → staging → prod. Each deploy:

- appends a new deployment row,
- validates that every required binding exists in the target environment,
- and, for a protected environment, requires admin or owner.

Gates based on eval results come later.
