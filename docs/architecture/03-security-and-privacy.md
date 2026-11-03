# 03 · Security, Threat Model, Checkpoint Protection, Erasure

## Authentication ([ADR-0005](../adr/0005-identity-and-tokens.md))

**Access token**
- ES256 JWT, valid for 10 minutes.
- Claims: `iss`, `aud`, `sub` (principal), `sid` (session), `jti`, `iat`, `exp`, `typ=access`.
- **No org or roles in the token.** Membership and role are looked up on every request, so role changes and removals take effect immediately.
- Verification selects the key by `kid`, pins the algorithm per key, and rejects `none` and every `HS*` algorithm.

**Refresh token**
- An opaque 256-bit value. Only its `sha256` is stored, together with `family_id` and `used_at`.
- Expiry: 7 days idle, 30 days absolute.
- Delivered in a cookie with `HttpOnly; Secure; SameSite=Strict; Path=/auth/refresh`, plus an Origin check and a custom-header check against CSRF.
- **Rotated on every use.** Presenting a used token revokes the whole family and the session, and writes the audit event `auth.refresh_reuse_detected`.

**Sessions**
- Stored in `sessions(id, user_id, revoked_at, …)`.
- Checked inside the per-request membership query, so revocation is immediate and does not depend on Redis.
- "Log out everywhere" revokes all of the user's sessions.

**Signing keys**
- States: `next`, `active`, `retiring`, `retired`.
- `/.well-known/jwks.json` publishes the next, active and retiring keys.
- Keys rotate every 90 days.
- Local development: PEM files encrypted with the vetted `cryptography` library. Phase 10: AWS KMS asymmetric signing.

**Passwords**
- argon2id (`argon2-cffi`).
- `user_identities(provider, issuer, subject)` is a separate table from day one, so password login is just one identity type and SSO can be added later.

**API keys**
- Format: `agk_{stage}_{key_id}_{secret}`, where `stage` is `live` or `test` (not an org environment) and `key_id` is public.
- Stored as `HMAC-SHA256(pepper, secret)`. The pepper lives in Secrets Manager (an env var locally).
- Each key is scoped to an org, an environment and a subset of permissions.
- `expires_at` is required, with a maximum of 1 year. The key is shown once.

**Login protection**
- Redis rate limits per IP and per email.

## Authorization

See the role matrix and structural guards in [01](01-tenancy-and-data.md#multi-tenant-model).

**Privilege-escalation guards**
- You can only grant roles at or below your own.
- Nobody can change their own role.
- The last owner cannot be removed (enforced with a row lock).
- Invites are bound to an email address, and the role is re-checked when the invite is accepted.
- API keys can never manage members, keys or credentials.
- Only admins can deploy to protected environments.
- The gateway rejects any tool or credential that is not in `version.bindings`.
- A trigger rejects any update to a published version.

## Credentials ([ADR-0011](../adr/0011-credentials-by-reference.md))

**CredentialReference fields:** org, env, name, kind (`api_key`, `oauth2`, `basic`, `db_dsn`), provider, `secret_path`, status, `rotated_at`, `expires_at`.
- `secret_path` addresses the secret, not a particular version of it, so rotation needs no row change.

**Storage path:** `aegisgrid/{stage}/orgs/{org}/envs/{env}/creds/{cred}` in Secrets Manager, tagged with org and env.

**IAM split**

| Task role | Permissions |
|---|---|
| api | Create, put, tag and delete secrets. **Explicitly denied `GetSecretValue`.** |
| worker | Get only. |

**Resolution**
- Secrets are resolved **only by the ToolGateway**, at the moment of the call.
- The worker keeps them in an in-process cache with a TTL of 5 minutes or less. They are never cached in Redis.
- Every resolved value is registered with a per-execution scrubber, which cleans tool output, exceptions and trace events.

**Never present in:** LangGraph state, checkpoints, spans, logs, or LLM context. Request models use `SecretStr`.

**Local development**
- The same `SecretStore` interface, backed by a `local_secrets` table in a separate schema.
- Values are encrypted with a vetted library (`cryptography`'s Fernet), using a key from an env var.
- The api-write-only IAM split cannot be enforced locally. This is accepted.

## Checkpoint protection ([ADR-0012](../adr/0012-checkpoint-protection.md))

These are verified facts about `langgraph-checkpoint-postgres` 3.2.0, checked against main on 2026-10-26:

| Fact | Consequence |
|---|---|
| `EncryptedSerializer` only encrypts what passes through `serde.dumps_typed` | That covers blobs and pending writes. It has no key id, so it cannot rotate keys. |
| `put()` stores `None`, `str`, `int`, `float` and `bool` channel values **inline as plaintext JSONB** in `checkpoints.checkpoint` | Primitive channels bypass encryption entirely. |
| Checkpoint `metadata` is plaintext JSONB that copies string and number values from `config["configurable"]` and `config["metadata"]` | Anything placed in the run config lands in plaintext. |
| `loads_typed` accepts unencrypted rows | Plaintext rows load without complaint, which allows a downgrade. That needs DB write access, so the risk is accepted. |

**Decision**
1. **Never implement cryptographic primitives.** Use a vetted, maintained library that does **envelope encryption**: a data key per message or cache window, wrapped by a KMS key.
   - Candidate: AWS Encryption SDK for Python. It has a KMS keyring for production and a raw AES keyring with local key material for free local development, with the same library and message format in both.
   - Alternative: Google Tink with an AWS KMS integration.
   - The final pick is verified in Phase 3 for Python 3.13 compatibility, maintenance status and data-key caching. Caching matters because KMS is billed per request.
2. **What AegisGrid owns:**
   - `CheckpointCipher`, an adapter that implements LangGraph's `CipherProtocol` and delegates to the library.
   - The cipher-name/key-id metadata, e.g. `esdk:v1`.
   - The rotation policy: re-wrap on rotation, and old keys stay decrypt-only.
   - The serialization boundary.
   - An **encryption context** of `{org_id, execution_id}`, taken from a contextvar the worker sets. This cryptographically binds each blob to its tenant and execution.
3. **State-schema rule:** every top-level channel is a container. Primitive top-level channels are allowed only for reserved, non-sensitive runtime fields. The spec compiler enforces this.
4. **No content in run `config`.** Only `thread_id`.
5. **Isolation:** a `runtime` schema accessible only to `aegis_worker`. Checkpoints are deleted 7 days after the execution reaches a terminal state, or immediately on erasure.
6. **Canary test:** a run writes a unique canary string, and the test asserts it never appears in plaintext anywhere in the `runtime` schema.

## Agent-specific threats

**Principle:** the LLM is an untrusted planner. Authority comes only from the version's bindings plus gateway policy.

- **Prompt injection (direct or indirect)**
  - Allow-listed tools with argument constraints.
  - Untrusted content is delimited and labelled as data.
  - **Taint tracking:** once an execution has ingested tool, MCP or memory output, high-risk tools and external writes require approval. Before Phase 7 they are denied outright.
  - Caps on steps, cost and output size.
- **Malicious tool output**
  - Size and type limits, HTML stripped, output tainted.
  - The UI never auto-renders image or link markdown, which closes an exfiltration channel.
- **MCP:** remote only, hash-pinned with drift blocked, sampling and elicitation disabled, a separate credential per server, no passthrough of user credentials.
- **SSRF:** resolve-then-pin connect, blocklists, redirects re-validated, allow-lists. An egress proxy comes later.
- **SQL injection:** named, parameterized queries only.
- **Memory poisoning:** provenance on every record, quarantine for untrusted records, TTLs.
- **Exfiltration via the model**
  - A per-org allow-list of model providers, with zero-retention providers for enterprise.
  - Egress allow-lists.
  - DLP comes later.

## Approvals, replay, spoofing

- **Approvals:** action-hash binding, a locked decision transaction that re-checks permissions, separation of duties, single-use consumption, expiry, and resume input built **only from the approval row** (see [02](02-execution-runtime.md#human-approval-architecture)).
- **Workers can't be spoofed:** they have no inbound ports and no callback API. No API endpoint writes spans, events, step results or checkpoints, and the `aegis_api` role has no INSERT grant on those tables.
- **Inbound webhooks** (triggers phase):
  - Per-trigger secret, header `X-Aegis-Signature: t=…,v1=HMAC(secret, t.raw_body)`.
  - ±5-minute window, constant-time compare.
  - Replays blocked by `UNIQUE(org, trigger, delivery_id)` in Postgres, kept for 24 hours.
  - Outbound webhooks are signed the same way and pass the SSRF guard.
- **Duplicate executions:** idempotency key plus request hash.

## Audit logging

**Recorded events**
- Authentication: login, login failure, refresh-token reuse.
- Organization access: members, roles, invites, keys.
- Credentials: create, rotate, delete.
- Agent lifecycle: publish, deploy, rollback.
- Approvals: requested, decided, consumed.
- Administration: settings and policy changes, cancels, exports, erasure (counts only), support access.

**Integrity**
- Written in the **same transaction** as the change it records.
- The app role has INSERT and SELECT only, and a trigger blocks UPDATE and DELETE.
- Partitioned monthly.
- Later: hourly digests anchored to S3 Object Lock.

**Retention:** 1 year by default, configurable up to 7 years.

**Separate from traces**
- Different table, retention and permission (`audit:read`, admin and above).
- Audit rows are never erased. User actors are pseudonymized instead.

## Data sensitivity and redaction

| Class | Contents | Storage |
|---|---|---|
| C0 | Metadata: ids, timings, tool names, token counts, status | Stored |
| C1 | Content: prompts, outputs, tool args and results | Stored, redaction applies |
| C2 | Sensitive: PII, end-user input, schema fields marked `x-sensitive` | Stored, redaction applies |
| C3 | Secrets | **Never stored** |

**How redaction works**
- Redaction runs **in the worker, before both the Postgres insert and the Redis publish**.
- First the secret scrubber: exact resolved values, plus patterns for AWS keys, JWTs, bearer tokens and PEM blocks.
- Then masking driven by each tool's schema.

**`payload_mode` per org and environment:** `full`, `redacted` or `metadata_only`. The default is `redacted` for protected environments and `full` for dev.

**Retention:** C1 and C2 for 30 days, C0 for 1 year.

**Logs:** application logs never contain payloads or request bodies. A deny-list log processor and a test enforce this.

## Data erasure ([ADR-0018](../adr/0018-data-erasure.md))

**Mechanism**
1. **`subject_key`:** an optional field on execution create, the caller's pseudonymous id for the data subject. It is propagated to memory writes and to S3 object tags.
   - Without it, erasure works per execution (`DELETE …/executions/{id}/payloads`) or through retention.
   - The API documentation states this requirement.
2. **Invariant:** content columns are nullable and erasable. Metadata is not.
3. **`POST /orgs/{org}/data-subjects/{key}/erase`** (owner or admin) enqueues an erasure job.
   - It runs in the worker, because only the worker can reach checkpoints.
   - It writes to `erasure_requests` and an audit event with counts only.

| Location | Action | Residual |
|---|---|---|
| `executions` input, output, error message | Set to NULL, set `erased_at` | None |
| `trace_spans` previews and content attributes | Content set to NULL, metadata kept | None |
| S3 payloads `orgs/{org}/executions/{exec}/…` | Delete the prefix. The payload bucket has **versioning off**, or noncurrent versions expire after 1 day | None |
| Checkpoints | Delete by `thread_id` immediately | None |
| `tool_calls` result refs, approval previews | Set to NULL or redact | None |
| `memory_records` | Hard `DELETE` by `subject_key` | None |
| `model_calls`, rollups | No content, kept | None |
| `audit_log` | Redacted by design. Actors pseudonymized on user deletion | None |
| Application logs | Never contain payloads. CloudWatch retention 30 days | Bounded |
| Redis | Ephemeral, TTLs, no persistence in prod | None |
| RDS backups / PITR (≤ 35 days) | `erasure_requests` is re-applied after any restore. Backups age out | Bounded by the backup window |
| LLM providers, external tools | Outside our control. Zero-retention settings and DPAs | Documented |

**Org deletion:** hard-delete every tenant row, the S3 prefix `orgs/{org}/`, the org's secrets path and its checkpoints. Per-org KMS crypto-shredding is a later option (paid).

**Timing**
- The `subject_key` column, the nullable-content invariant and the S3 layout ship in Phase 3.
- The erasure job ships in Phase 6.
- The 30-day content retention is the backstop.

## Threat table

| # | Risk | Mitigation | When |
|---|---|---|---|
| 1 | Cross-org read/write from a missing filter | `OrgContext` repos, a raising loader guard, the 404 meta-test | Phase 1 |
| 2 | Child row points at another org's parent | Composite FK `(org_id, id)` | Phase 1 |
| 3 | No second line of defense | RLS (FORCE), transaction-local `set_config`, non-bypass roles | Phase 9.5 |
| 4 | IDOR or id enumeration | Authorization is the control. Cross-org returns 404. UUIDv7 is not relied on | Phase 1 |
| 5 | Stolen access token | 10-minute TTL, session check on every request | Phase 1 |
| 6 | Refresh token theft or replay | Rotation, family revocation, HttpOnly + Strict cookie | Phase 1 |
| 7 | Signing key compromise | `kid` rotation, JWKS overlap, KMS signing | Phase 1 / 10 |
| 8 | API key leak | Scannable prefix, HMAC storage, scope, expiry, revocation | Phase 1 |
| 9 | Self-elevation, last-owner lockout, invite abuse | Grant at or below own role, owner lock, hashed email-bound invites | Phase 1 |
| 10 | Brute-force login | Redis rate limit per IP and per email | Phase 1 |
| 11 | Secrets in state, traces or logs | Reference ids only, resolution in the gateway only, scrubber, `SecretStr`, log deny-list | Phase 4 |
| 12 | API process exfiltrates secrets | IAM split | Phase 10 |
| 13 | Plaintext content in checkpoints | Envelope encryption via a vetted library, state-schema rule, canary test | Phase 3 |
| 14 | Prompt injection triggers side effects | Allow-lists, taint → deny (Phase 4), taint → approval (Phase 7) | Phase 4 / 7 |
| 15 | Malicious tool output | Limits, stripping, taint, no markdown auto-render | Phase 4 / 9 |
| 16 | MCP rug-pull or poisoning | Hash pins, block on drift, no sampling | Phase 4 |
| 17 | Malicious stdio MCP server | Disallowed | Phase 4 |
| 18 | SSRF / IMDS / DNS rebinding | Resolve-then-pin, blocklist, redirect re-check | Phase 4 |
| 19 | SQL or tool injection | Named parameterized queries | Phase 4 |
| 20 | Memory poisoning | Provenance, quarantine, TTL | Phase 7b |
| 21 | Cross-org vector leakage | Filter in SQL `WHERE` | When pgvector is adopted |
| 22 | Approval bypass or bait-and-switch | Action hash, locked decision, single-use consumption | Phase 7a |
| 23 | Forged resume or forged traces | Worker-only writes, grants, resume built from the approval row | Phase 3 / 7a |
| 24 | Duplicate side effects | Checkpoint resume plus ledger, no auto-retry of ambiguous writes | Phase 5 |
| 25 | Webhook forgery or replay | HMAC, timestamp window, `delivery_id` stored in Postgres | Triggers phase |
| 26 | Audit tampering | INSERT-only grants plus trigger, S3 anchoring later | Phase 1 / later |
| 27 | Incomplete erasure | `subject_key`, nullable content, erasure job, restore ledger | Phase 3 / 6 |
| 28 | Approver account takeover | MFA for owners and approvers | Phase 9.5 |
| 29 | Uncontrolled egress at scale | Egress proxy, private subnets | Later |
