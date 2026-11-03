# ADR-0004: Database roles

**Status:** Accepted, 2026-10-26 (fixes validation finding C1)

## Context

Two security properties need enforcement at the database level, not just by convention:

- Checkpoints are readable only by the worker.
- The API cannot write traces or execution results.

Later, row-level security (RLS) needs roles that do not bypass it. One shared application role can provide none of this.

## Decision

| Role | Purpose | Notes |
|---|---|---|
| `aegis_migrator` | Owns tables and runs Alembic | Used only by migrations |
| `aegis_api` | The api process | No access to the `runtime` schema. No INSERT on `trace_spans`, `execution_events`, `model_calls` or `tool_calls`. Cannot UPDATE or DELETE `audit_log` |
| `aegis_worker` | The worker process | Only role allowed to use the `runtime` (checkpoint) schema |
| `aegis_system` | Job claimer and reaper only, through a separate engine | Has `BYPASSRLS`. Never used for request handling or auth lookups |

- **Creation:** a bootstrap script under `infra/db/` creates the roles. Locally it runs via the compose `docker-entrypoint-initdb.d` mount; on AWS it is a one-off task. Migrations then grant privileges.
- **Phase 1 scope:** creates the roles and grants. Request code runs as `aegis_api` from the start.

## Consequences

- Turning on RLS later (Phase 9.5) is just a migration.
- Local development needs a compose volume reset when the bootstrap script is first added (`docker compose down -v`).

## Alternatives rejected

- **A single app role:** the properties above become unenforceable.
- **Creating roles inside Alembic migrations:** migrations would need superuser rights and would mix role passwords into migration code.
