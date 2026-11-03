# ADR-0008: Declarative workflow spec compiled to LangGraph; own ModelClient

**Status:** Accepted 2026-10-26

## Context

Tenants define workflows, and those workflows run on shared workers. Each workflow must be safe to run, versionable and checkable before it runs. Cost accounting has to be exact.

## Decision

### Workflow spec

- Workflows are **JSON specs**, not user Python. A spec contains:
  - `schema_version`
  - `state_schema`
  - `entry`
  - `nodes`, each typed as one of `llm`, `tool`, `router`, `approval_gate`, `memory_read`, `memory_write` or `end`
  - `edges`
- Router conditions use **JMESPath**. There is no `eval`.
- Loops need `max_iterations`. Every run also has `max_steps` and `max_cost` limits.
- The agentic tool loop compiles to an `llm` node and a `tool` node in a loop. Each tool call is therefore its own checkpointed super-step.

### Compilation

- Specs compile to a LangGraph `StateGraph`.
- Compiled graphs are cached per process in an LRU keyed by `(content_hash, runtime_version)`.
- Compilation checks:
  - reachability
  - that loops are bounded
  - that every binding is present
  - the state-schema rule from ADR-0012

### Model access

- We own a thin `ModelClient.generate(messages, tools, schema, params) -> ModelResult` over the provider SDKs. It returns normalized usage, the finish reason and a raw reference.
- LangGraph is used only for the graph, checkpointing and interrupts.

## Consequences

- Specs are safe in multi-tenant use, diffable and hashable.
- Token and cost data is exact, including cached and reasoning tokens.
- Traces keep the raw request and response.
- Power users can't write arbitrary code. A sandboxed code tool may come later.
- The runtime is coupled to the LangGraph version. We pin that version and record it as `runtime_version`.

## Alternatives rejected

- **User-supplied Python graphs:** amounts to remote code execution.
- **LangChain chat model classes:** the API churns, and they give lossy usage accounting.
