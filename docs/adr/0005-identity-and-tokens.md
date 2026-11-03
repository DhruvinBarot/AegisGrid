# ADR-0005: Identity model and tokens

**Status:** Accepted 2026-10-26. Own auth behind an `IdentityProvider` interface was confirmed for Phase 1; the password provider is MVP-only.

## Context

The platform needs browser sessions and machine callers. It has to support SSO later. Membership and role changes must take effect immediately, and local development should cost nothing to run.

## Decision

### Identity tables

- `users` stores people.
- `user_identities(provider, issuer, subject)` stores how they log in. A password is just one identity type, so OIDC and SAML can be added later.
- `principals` (types `user` and `service`) are what actually hold memberships. That puts service accounts on the same RBAC path as humans.

### Tokens and sessions

- **Access token:** ES256 JWT, valid for 10 minutes. Claims are `sub`, `sid`, `jti`, `iat`, `exp`, `aud`, `iss` and `typ`. It carries **no org and no roles**.
- **Refresh token:** an opaque token, stored only as a hash, rotated on every use.
  - Reusing an old token revokes the whole token family and the session.
  - It lives in an `HttpOnly; Secure; SameSite=Strict` cookie scoped to the `/auth/refresh` path.
- **Session check:** every request joins `sessions` to check for revocation. This adds no extra round trip and has no Redis dependency.
- **Signing keys:** identified by `kid` and published at `/.well-known/jwks.json`.
  - States: next, active, retiring.
  - Rotated every 90 days.
  - Verification pins the algorithm per key.
  - Locally the keys are encrypted with `cryptography`. Phase 10 moves signing to KMS.

### Credentials and API keys

- **Passwords:** argon2id via `argon2-cffi`.
- **API keys:** formatted `agk_{live|test}_{key_id}_{secret}` and stored as an HMAC under a pepper. Each key is scoped to an org, an environment and a set of permissions, expires after at most one year, and is shown only once.

### Deferred

- Email verification and email delivery. Invite tokens are returned to the inviter to share.
- MFA (Phase 9.5).
- SSO and SCIM.

## Consequences

- Role changes and session revocation take effect immediately.
- We own the security-sensitive auth code. Mitigations:
  - well-tested libraries
  - tests for refresh reuse, algorithm pinning and the expired, revoked and wrong-audience cases

## Alternatives rejected

- **Org or role claims in the JWT:** revocation would be stale for the token's lifetime.
- **HS256:** shares the secret with every verifier.
- **A managed IdP:** a paid dependency plus vendor coupling. It can be revisited when SSO is built.
