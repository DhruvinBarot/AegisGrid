# ADR-0013: psycopg 3 as the single PostgreSQL driver

**Status:** Accepted, 2026-10-26 (resolves validation finding C6)

## Context

Phase 0 uses `asyncpg`. `langgraph-checkpoint-postgres` (3.2.0) requires `psycopg>=3.2` and `psycopg-pool`. Using both drivers would mean two driver stacks and two pool types, and two different behaviors for prepared statements and LISTEN/NOTIFY.

## Decision

- Use psycopg 3 for all database access.
- The SQLAlchemy URL becomes `postgresql+psycopg://…`.
- The checkpointer uses `psycopg-pool`.
- The switch lands at the start of Phase 1, before any models exist.

The switch changes these files:

| File | Change |
|---|---|
| `pyproject.toml`, `uv.lock` | Swap `asyncpg` for `psycopg[binary]` |
| `app/core/config.py` | Default URL |
| `app/core/db.py` | `connect_args`: `timeout` becomes `connect_timeout` |
| `tests/test_health.py` | Dead-engine URL |
| `docker-compose.yml` | `AEGIS_DATABASE_URL` |
| `README.md` | Configuration table |

## Consequences

- One driver to tune and debug.
- Raw async throughput is somewhat lower than asyncpg. That doesn't matter here, because LLM latency dominates.
- PgBouncer in transaction mode requires disabling or managing prepared statements. This is revisited when PgBouncer is introduced.

## Alternatives rejected

- **Keeping asyncpg for the app and adding psycopg for the checkpointer:** two stacks in the worker.
