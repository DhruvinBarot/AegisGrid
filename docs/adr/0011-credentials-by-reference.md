# ADR-0011: Credentials by reference

**Status:** Accepted, 2026-10-26

## Context

Tools need secrets, but secrets must never appear in configuration tables, versions, workflow state, checkpoints, traces, logs, or the context sent to the LLM.

## Decision

### Storage

- Tables store only a `credential_references` row:
  - org, env, name, kind, provider
  - `secret_path`
  - status, `rotated_at`, `expires_at`
- The secret value itself lives in AWS Secrets Manager at `aegisgrid/{stage}/orgs/{org}/envs/{env}/creds/{cred}`.
- Local development uses the same `SecretStore` interface backed by a `local_secrets` table, encrypted with `cryptography`'s Fernet. No custom crypto.

### Resolution

- Secrets are resolved **only inside ToolGateway**, at the moment of the call.
- The worker keeps them in an in-process cache with a TTL of 5 minutes or less.
- Each resolved value is registered with a per-execution scrubber.

### IAM split (Phase 10)

- The api task role can create, put and delete secrets, and is **explicitly denied** `GetSecretValue`.
- The worker task role can only get secrets.

### Rotation

- Rotation creates a new `AWSCURRENT` version in Secrets Manager. No database row changes.

## Consequences

- If the API process is compromised, execution secrets are not exposed.
- If secrets ever leaked into state, fixing it would mean scrubbing historical data. That cost is why this is a hard-to-reverse decision.

## Alternatives rejected

- **Secrets encrypted in app tables:** the app process becomes key custodian and the blast radius grows.
- **Secrets copied into versions:** history would hold frozen, leaked secrets.
