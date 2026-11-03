# 05 · API, Frontend, AWS Deployment

## API design

**Conventions**
- Prefix `/v1`, JSON bodies, cursor pagination.
- Errors use RFC 9457 `application/problem+json`.
- Creates accept `Idempotency-Key`. Draft edits use `draft_rev` (optimistic locking).
- The tenant is **always in the path**. A body field named `organization_id` is never accepted.
- Public ids are type-prefixed: `org_`, `agt_`, `ver_`, `dep_`, `exe_`, and so on.
- Paginated lists (audit log, agents, versions, deployments) return `{items, next_cursor}`, newest first. The cursor is opaque: pass `?cursor=<next_cursor>&limit=` for the next page.

✅ marks rows implemented so far. The live, authoritative reference is the OpenAPI UI at `/docs`.

| Resource | Representative endpoints | Permission |
|---|---|---|
| Auth | `POST /auth/register`, `/auth/login`, `/auth/refresh` (cookie), `/auth/logout`, `/auth/logout-all`; `GET /.well-known/jwks.json` | public or session |
| Me | `GET /me`, `GET /me/organizations` | authenticated |
| Orgs | `POST /orgs`, `GET/PATCH/DELETE /orgs/{org}` | member / admin / owner |
| Members | `GET /orgs/{org}/members`, `PATCH/DELETE …/members/{id}`, `POST …/invitations`, `GET …/invitations`, `DELETE …/invitations/{id}`, `POST /invitations/accept` | `members:*` |
| Service accounts / keys | `POST …/service-accounts`, `POST …/service-accounts/{id}/keys` (secret shown once), `DELETE …/keys/{id}` | `apikeys:*` |
| Environments | `GET/POST/PATCH …/environments` | `environments:*` |
| Audit | `GET …/audit-log` | `audit:read` (admin and above) |
| Agents ✅ | `GET/POST …/agents`, `GET/PATCH …/agents/{id}`, `GET/PUT …/agents/{id}/draft` (`draft_rev` lock), `POST …/agents/{id}/draft/validate` (dry run) | `agents:read` / `agents:write` |
| Versions ✅ | `POST …/agents/{id}/versions` (publish), `GET …/versions`, `GET …/versions/{v}` (with spec). Diff `GET …/versions/{a}/diff/{b}` not built yet | `agents:publish` / `agents:read` |
| Deployments ✅ | `POST …/environments/{env}/deployments {version_id}`, `GET …/agents/{id}/deployments?environment_id=`, `POST …/deployments/{id}/rollback` | `agents:deploy`, plus `environments:protected` (owner/admin) for protected environments |
| Executions | `POST …/executions {agent_id, environment, input, subject_key?}` with optional `?wait=`; `GET …/executions`; `GET …/executions/{id}`; `POST …/{id}/cancel`; `POST …/{id}/retry`; `POST …/{id}/replay`; `DELETE …/{id}/payloads` | `executions:create:{env}` / read / cancel |
| Trace | `GET …/executions/{id}/trace`, `GET …/executions/{id}/events` (SSE), `GET …/spans/{id}/payload` (authorized; presigned URL ≤ 60 s) | `executions:read` |
| Approvals | `GET …/approvals?status=pending`, `GET …/approvals/{id}`, `POST …/approvals/{id}/decision` | `approvals:decide` + separation of duties |
| Integrations | `GET/POST …/integrations`, `PUT …/integrations/{id}/bindings/{env}`, `POST …/integrations/{id}/discover`, `GET …/integrations/{id}/tools` | `integrations:*` |
| Credentials | `POST …/credentials` (value is write-only), `POST …/credentials/{id}/rotate`, `GET` (metadata only), `DELETE` | `credentials:*` |
| Model providers | `GET/POST …/model-providers` | admin |
| Evaluations | `…/eval-datasets` (+ `/cases`), `…/evaluators`, `POST …/eval-runs`, `GET …/eval-runs/{id}`, `GET …/eval-runs/compare?a=&b=` | `evals:*` |
| Usage | `GET …/usage?group_by=agent,version,model&from=&to=` | `usage:read` |
| Data subjects | `POST …/data-subjects/{key}/erase` | owner / admin |

**Endpoints that deliberately do not exist:** creating approvals, writing traces, writing execution status, and reading checkpoints. Only the worker produces those.

## Frontend

**Stack and auth**
- Next.js App Router, TypeScript, TanStack Query, and a client generated from the FastAPI OpenAPI schema with `openapi-typescript`.
- Routes are `/{orgSlug}/…`.
- The access token is held in memory only. The refresh token is an HttpOnly, `SameSite=Strict` cookie; the app and API sit on sibling subdomains.

**Safe rendering:** model and tool output is shown as plain text or sanitized markdown. Images and links are never auto-loaded.

| Page | MVP | Later |
|---|---|---|
| Login / register / accept invite | ✓ | SSO, MFA |
| Org selector and create org | ✓ | |
| Dashboard (executions, failures, pending approvals, cost today) | basic tiles | charts, SLOs, alerts |
| Agents list and agent detail (deployments per env, rollback) | ✓ | |
| Agent builder (structured form, JSON spec editor, tool picker, validation) | ✓ | visual graph editor |
| Version history and diff | ✓ | |
| Executions list | ✓ | saved views |
| Execution detail and trace (span tree, timings, tokens, cost, previews, live SSE) | ✓ | replay UI |
| Approvals inbox and decision view (shows the hash-bound action) | ✓ | Slack and email approvals |
| Integrations and MCP connections (bindings per env, discovery, pin-drift alerts) | ✓ | |
| Credentials (write-only) | ✓ | |
| Evaluations (datasets, runs, compare) | basic | human ratings, gates |
| Members, invites, service accounts and keys | ✓ | SCIM |
| Settings (environments, payload mode, budgets) | basic | |
| Usage and cost | table | breakdowns, budgets UI |
| Audit log | ✓ | export |

## AWS deployment (Phase 10, everything here is **PAID**)

**Network**
- One VPC across two availability zones.
- Public subnets hold the ALB. Private subnets hold the ECS tasks. Isolated subnets hold RDS and ElastiCache.
- Worker egress to LLMs and external tools goes through a NAT gateway (paid).
- VPC endpoints serve Secrets Manager, KMS, S3, ECR and CloudWatch Logs.

**Compute (ECS Fargate)**
- `api` sits behind the ALB.
- `worker` has no inbound ports and scales on queue depth and age.
- `web` runs Next.js. Amplify or Vercel would also work.

**Data**
- RDS PostgreSQL 17 with pgvector. The pgvector version must be ≥ 0.8.3; this is provisional and gets verified against the RDS minor version.
- Multi-AZ for production, with PITR and backups.
- ElastiCache Redis with TLS and AUTH.

**Secrets and encryption**
- Secrets Manager, with the IAM split described in [03](03-security-and-privacy.md#credentials).
- KMS keys for checkpoint envelope encryption, S3, RDS, and later JWT signing.
- S3 buckets: a payload bucket (versioning off, or noncurrent versions expire after one day) and an export bucket.

**Edge and monitoring**
- ACM certificates, an HTTPS-only ALB, and Route 53. WAF is optional.
- CloudWatch logs (30-day retention) and metrics.
- Alarms on queue age, lease expiries, 5xx responses, and RDS CPU and connection count.

**Pipeline**
1. GitHub Actions authenticates to AWS with OIDC, so no long-lived keys exist.
2. Lint, typecheck and test against a real Postgres.
3. Build images and push them to ECR.
4. Run `alembic upgrade` as a one-off ECS task, connected as `aegis_migrator`.
5. Deploy `api`, then `worker`, as rolling deploys.

- Migrations follow **expand/contract**, because old workers may still be running during a deploy.
- On SIGTERM a worker stops claiming new work. Lease expiry plus checkpoint resume cover any long runs that get interrupted.
- Infrastructure is defined with Terraform or OpenTofu under `infra/terraform/`.

**Growth path** (no Kubernetes or microservices required)
1. Add more worker tasks, then split into separate worker pools by job kind or tenant tier.
2. Put PgBouncer or RDS Proxy in front of Postgres. Watch the LISTEN and prepared-statement caveats.
3. Add a read replica for dashboards.
4. Move the queue to SQS behind the existing `JobQueue` interface, keeping the outbox row.
5. Move traces and analytics to ClickHouse, or to S3 plus Athena.
6. Split the execution plane into its own deployable. The module seam already allows this.
7. Introduce cells for large tenants.
