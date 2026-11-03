# ADR-0001: Modular monolith with `api` and `worker` processes

**Status:** Accepted, 2026-10-26

## Context

AegisGrid needs two things kept apart: a control plane (configuration and governance) and an execution plane (agent runs). It is built by a small team. Many writes must stay atomic across tenant, version, execution and job data.

## Decision

- **One codebase.** A single Python codebase under `backend/app/modules/*` runs as two processes:
  - `api`: the FastAPI control plane and HTTP edge.
  - `worker`: the execution plane.
- **The seam.** The `executions` module (records, state machine, `JobQueue` protocol) is the only shared seam. Control-plane modules must never import `runtime`, `gateway` or `memory`. An import-boundary test enforces this.
- **Cross-module calls** go through the target module's `service.py`.

## Consequences

- One deploy, one database transaction boundary, and simple local development.
- The execution plane can become its own deployable later without a rewrite.
- Discipline is needed to keep the module boundaries, which is why they are enforced by a test.

## Alternatives rejected

- **Microservices.** They add network failure modes, distributed transactions and operational cost, with no load to justify them.
