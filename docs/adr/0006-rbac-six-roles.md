# ADR-0006: RBAC with six roles

**Status:** Accepted, 2026-10-26. This fixes validation finding C2 (service accounts needing admin) and C4 (protected deploy requests).

## Context

Enterprises separate three kinds of work: building agents, running production, and business sign-off. Service accounts that run production executions must not need admin rights.

## Decision

The roles are `owner`, `admin`, `developer`, `operator`, `approver` and `viewer`. The full matrix is in [01](../architecture/01-tenancy-and-data.md#multi-tenant-model).

- **One role per membership.** The map from role to permissions lives in versioned code, not in the database.
- **`operator`** can create, cancel and read executions in **any** environment and can read configuration. It cannot write configuration or manage members. It is the normal role for service accounts.
- **Deploying to a protected environment** requires admin or owner. A request/approve flow for deploys is deferred.
- **Granting rules:**
  - You can grant only roles at or below your own.
  - Nobody can change their own role.
  - The last owner is protected under a row lock.
- **Separation of duties.** Whoever started an execution cannot approve its actions. A single-owner org can opt into self-approval, but only in non-protected environments, and each such decision is audit-flagged.

## Consequences

- Production automation runs with least privilege.
- There is one more role to test. The cross-org and role-grant tests cover it.

## Alternatives rejected

- **Five roles (no operator):** service accounts would need admin.
- **Per-environment permission grants:** more flexible, but more complex than the MVP needs.
