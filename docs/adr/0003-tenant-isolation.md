# ADR-0003: Structural tenant isolation

**Status:** Accepted, 2026-10-26

## Context

AegisGrid must prevent any cross-organization read, write, tool access, memory access or execution access. A single forgotten `WHERE organization_id = …` must not be able to leak data.

## Decision

- **Every tenant-owned table:**
  - has `organization_id NOT NULL` and `UNIQUE(organization_id, id)`
  - references its parent through a composite FK `(organization_id, parent_id)`
  - has indexes that start with `organization_id`
- **How the tenant is determined:** from the URL org plus a live membership lookup. Never from the token or the request body.
- **Cross-org access** returns **404**.
- **Code-level guards:**
  - Repositories take an `OrgContext`.
  - A `do_orm_execute` hook applies `with_loader_criteria` and **raises** if a tenant model is queried without org context. The only escape is an explicit `system_scope()`.
  - `session.get()` is banned on tenant models.
- **CI gate:** a mandatory cross-org meta-test seeds orgs A and B, calls every `/orgs/{org_id}/…` route as a user of A with B's ids, and asserts 404.
- **Postgres RLS:**
  - Enabled at Phase 9.5, before the first external tenant.
  - Uses `FORCE`, with the org set transaction-locally via `set_config('app.org_id', …, true)`.
  - CI then runs under the non-bypass roles (ADR-0004).
- **Documented exception:** the LangGraph checkpoint tables have no `organization_id`. ADR-0012 covers how they are protected instead.

## Consequences

- A cross-tenant reference cannot be represented in the schema.
- Retrofitting this later would mean rewriting every table, which makes it the hardest decision in the system to reverse.
- Until Phase 9.5, isolation rests on application code. The guard plus the meta-test is what makes that acceptable.

## Alternatives rejected

- **Isolation through indirect joins only:** fragile.
- **RLS on day one:** slows MVP iteration. The roles it needs are created now regardless.
- **Schema-per-tenant or database-per-tenant:** heavy operationally for the MVP.
