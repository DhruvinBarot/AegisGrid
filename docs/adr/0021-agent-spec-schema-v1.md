# ADR-0021: Agent spec schema v1

**Status:** Accepted, 2026-11-02. Closes the open decision "Spec schema v1 details" (roadmap, before Phase 2). Refines [ADR-0008](0008-declarative-spec-and-model-client.md) for the first three node types.

## Context

Published versions are permanent ([ADR-0007](0007-immutable-agent-versions.md)): every future runtime must still read a v1 spec. So v1 has to be small, strict and unambiguous. It must also never let through a spec the Phase 3 compiler would have to refuse. Phase 2 ships only `llm`, `router` and `end` nodes. Tools, approval gates and memory come in later phases.

## Decision

### Shape

```json
{
  "schema_version": 1,
  "metadata": {"description": "Ticket triage", "tags": ["support"]},
  "entry": "classify",
  "state_schema": {"type": "object", "properties": {"triage": {"type": "object"}}},
  "limits": {"max_steps": 25},
  "nodes": [
    {"id": "classify", "type": "llm", "config": {
      "model": {"provider": "ollama", "name": "llama3.2:3b", "params": {"temperature": 0}},
      "system": "You triage tickets.",
      "prompt": "Ticket: {{ input.body }}",
      "output_key": "triage",
      "output_schema": {"type": "object", "properties": {"urgent": {"type": "boolean"}}}
    }},
    {"id": "route", "type": "router"},
    {"id": "escalate", "type": "end", "config": {"output": "triage"}},
    {"id": "done", "type": "end"}
  ],
  "edges": [
    {"from": "classify", "to": "route"},
    {"from": "route", "to": "escalate", "when": "triage.urgent == `true`"},
    {"from": "route", "to": "done"}
  ]
}
```

| Field | Rule |
|---|---|
| `schema_version` | Literal `1` |
| `metadata` | Optional: `description` (≤ 2000 chars) and `tags` (≤ 20) |
| `entry` | Must name a node |
| `state_schema` | A valid JSON Schema (2020-12) with `type: object`. Defaults to `{"type": "object"}` |
| `limits.max_steps` | 1–200, default 25. `max_cost` arrives with prices in Phase 6 |
| `nodes` | 1–50 nodes. Ids match `^[a-z][a-z0-9_]{0,63}$` and are unique |
| `edges` | At most 200: `{from, to, when?}` |

**Node types**

| Type | `config` | Outgoing edges |
|---|---|---|
| `llm` | `model {provider, name, params}`, optional `system`, required `prompt`, required `output_key`, optional `output_schema` | Exactly one, without `when` |
| `router` | Empty: conditions live on the edges | One or more. Exactly one has no `when`; that one is the default |
| `end` | Optional `output`: JMESPath over the state. Omitted means the whole state | None |

- **Model:** `provider` is one of `fake`, `ollama`, `anthropic`, `openai`. `name` matches `^[A-Za-z0-9][A-Za-z0-9._:/@-]*$`, at most 128 chars. `params` may hold only `temperature` (0–2), `top_p` (0–1], `max_tokens` (1–200 000) and `stop` (≤ 4 strings).
- **Expressions:** JMESPath everywhere (ADR-0008): router `when`, end `output`, and `{{ … }}` placeholders in `prompt` and `system`. Each expression is compiled at validation, so syntax errors are caught before publish. A router takes the first edge whose `when` is truthy, else its default edge.
- **State rule (ADR-0012):** every top-level state channel declared in `state_schema` must be `object` or `array`. `input` is reserved: it holds the execution input and is always an object. An `llm` node writes an object to `output_key`. That object is shaped by `output_schema` when one is given (it must be an object schema), and is `{"text": …}` otherwise.
- **Graphs are acyclic in v1:** every node must be reachable from `entry`. With the edge rules above, every path then ends at an `end` node. Loops need `max_iterations` (ADR-0008) and come with the tool loop in Phase 4.

### Strictness

- Unknown fields are rejected at every level, and types are never coerced (`"100"` is not an integer).
- Drafts may hold any JSON object. Validation is a dry run (`POST …/draft/validate`). Publishing re-runs it and refuses an invalid draft with `422 invalid_spec` and the same error list.
- Each error is `{path, code, message}`, for example `{"path": "edges[2].when", "code": "invalid_jmespath", …}`.

### Canonical form and hash

- On publish the spec is **normalized**: parsed and dumped with every default filled in, so `{}` and `{"max_steps": 25}` mean the same thing. That normalized form is stored as the version's `spec`.
- `content_hash = "sha256:" + sha256(RFC 8785 JCS bytes of the normalized spec)`. JCS comes from the maintained `rfc8785` package, never hand-rolled. The hash covers `schema_version`.
- The canonical form is limited to 256 KiB. Numbers outside JCS's safe range (integers beyond ±2^53) are refused with `not_canonicalizable`.

### Evolution

- Adding an *optional* field with a default that doesn't change behaviour may stay in v1. Anything that changes meaning bumps `schema_version`, and the runtime keeps reading every version it ever accepted.
- New node types (`tool`, `approval_gate`, `memory_read`, `memory_write`) and loops each arrive in their phase, as additive extensions.

## Consequences

- A published spec can't fail Phase 3 compilation on structure, expressions or state shape. The compiler only adds runtime checks: bindings, and whether the chosen provider is configured in that environment.
- Hashes are stable across key order, whitespace and explicitly written defaults. A golden-hash unit test catches any change to normalization.
- Specs are deliberately narrow in v1. Authors can't loop or call tools until those phases land.

## Alternatives rejected

- **A separate `agent_drafts` table:** ADR-0007 already puts the single draft on `agents` (`draft_spec`, `draft_rev`).
- **Hashing the raw draft:** the same meaning would get different hashes depending on which defaults were written out.
- **Hand-written canonical JSON:** easy to get subtly wrong on numbers and Unicode.
- **Cycles in v1 guarded only by `max_steps`:** unbounded loops would be legal and costly. Bounded loops come with `max_iterations`.
- **A custom template language for prompts:** JMESPath is already the expression language. One language means one validator.
