# AegisGrid

Multi-tenant control plane for enterprise AI agents. FastAPI + PostgreSQL + Redis + Next.js. The agent runtime (LangGraph) arrives in Phase 3 and is **not installed yet**.

## Status

| Phase | Scope | State |
|---|---|---|
| 0 | Foundation: compose, FastAPI, Alembic, CI | Done |
| 1 | Identity, orgs, six-role RBAC, service accounts and API keys, environments, audit log, tenant isolation | Done, awaiting sign-off |
| 2 | Agents, drafts, spec v1 validation, immutable content-hashed versions, deployments and rollback | Done, awaiting sign-off |
| 3+ | Execution (worker, LangGraph, model calls), tools, tracing, approvals, evals, UI | Not started |

**Published agents can't run yet.** You can create, validate, publish, deploy and roll back agents, but executing one needs Phase 3. The frontend is still a placeholder page. Details: [roadmap](docs/architecture/06-roadmap.md), spec format: [ADR-0021](docs/adr/0021-agent-spec-schema-v1.md).

## Layout

```
backend/            FastAPI app (app/), Alembic migrations, pytest suite
frontend/           Next.js (App Router, TypeScript)
infra/docker/       Dockerfiles
docs/               Architecture (docs/architecture/) and decision records (docs/adr/)
docker-compose.yml  Local stack: postgres (pgvector), redis, api, frontend
```

## Prerequisites (all free)

- Docker Desktop
- Python 3.13 and [uv](https://docs.astral.sh/uv/) (`pip install uv`) — only for running backend tooling on the host
- Node 24 and pnpm 11 — only for running the frontend on the host

## Run everything in Docker

```bash
cp .env.example .env        # optional for Docker; needed for tooling on the host
docker compose up -d --build
docker compose run --rm migrate   # apply migrations, first run and after pulling new ones
```

| Service  | URL                              |
|----------|----------------------------------|
| API      | http://localhost:8000/healthz, `/readyz`, `/docs` |
| Frontend | http://localhost:3000            |
| Postgres | localhost:**5433** (app role: `aegis_api`/`aegis_api`, db: `aegisgrid`) |
| Redis    | localhost:6379                   |

Every port is bound to 127.0.0.1. Postgres is published on 5433 so it doesn't collide with a locally installed Postgres. Change any host port in `.env`.

The first start of an empty Postgres volume runs `infra/db/init/`, which creates the four application roles and the `aegisgrid_test` database. If your volume predates that script, reset it (see below).

Code changes in `backend/` and `frontend/` hot-reload inside the containers (polling is enabled for Windows bind mounts).

## Tests, lint, types

Tests run on the host against the compose services. They migrate `aegisgrid_test` themselves and include the cross-org 404 gate that CI enforces. They never touch the dev database, and they use Redis database 15, so the dev app's Redis keys (database 0) are left alone. Tests roll back every write: the test database must contain no committed rows, or the tenancy tests fail.

```bash
docker compose up -d --wait postgres redis

cd backend
uv sync
uv run pytest
uv run ruff check .
uv run pyright

cd ../frontend
pnpm install
pnpm typecheck
```

## Migrations

After pulling changes that add Python dependencies (Phase 2 added `rfc8785`, `jmespath` and `jsonschema`), rebuild the backend images first. The venv lives inside the image:

```bash
docker compose build api migrate
docker compose run --rm migrate                  # apply

cd backend
AEGIS_ENV=development uv run alembic revision -m "describe change"     # create
```

Migrations run as `aegis_migrator`, from `AEGIS_MIGRATOR_DATABASE_URL`, or from the local default when `AEGIS_ENV=development`. Alembic reads both from the process environment, not from `.env`. The API process never gets those credentials, so `alembic` does not work inside the `api` container. `alembic.ini` deliberately has no URL.

## Configuration

Backend settings are environment variables with the `AEGIS_` prefix (see `backend/app/core/config.py`), read from the process env or `.env`:

| Variable                      | Default (host)                                                         |
|-------------------------------|------------------------------------------------------------------------|
| `AEGIS_ENV`                   | **Required.** `development`, `test`, `staging` or `production`         |
| `AEGIS_DATABASE_URL`          | `postgresql+psycopg://aegis_api:aegis_api@127.0.0.1:5433/aegisgrid`    |
| `AEGIS_REDIS_URL`             | `redis://127.0.0.1:6379/0`                                             |
| `AEGIS_MIGRATOR_DATABASE_URL` | Migrations only, process environment only. Dev default uses `aegis_migrator` |

Staging and production also require real values for these. The app refuses to start while any of them has its local-dev default:

| Variable                       | Requirement                          |
|--------------------------------|--------------------------------------|
| `AEGIS_SIGNING_KEY_PASSPHRASE` | Encrypts the JWT signing keys        |
| `AEGIS_API_KEY_PEPPER`         | Keys the HMAC of API-key secrets     |
| `AEGIS_DATABASE_URL`           | Not the dev URL                      |
| `AEGIS_JWT_ISSUER`             | Not the dev issuer                   |
| `AEGIS_COOKIE_SECURE`          | `true`                               |
| `AEGIS_ALLOWED_ORIGINS`        | JSON list with no localhost origins  |

Behaviour that depends on `AEGIS_ENV`:

- **Invitation tokens** are returned to the inviter in `development` and `test` only. There is no email delivery yet, so invitations can't be delivered in staging or production.
- **Login rate limiting** fails closed (503) in staging and production when Redis is down. In development and test it fails open with a warning.

`/readyz` returns 503 only when Postgres is unreachable; Redis is reported but never gates readiness (Redis holds no durable state).

## Reset local data

```bash
docker compose down -v
```
