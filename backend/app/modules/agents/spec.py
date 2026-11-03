"""Agent spec schema v1 (ADR-0021): parse, validate, canonicalize, hash. Pure, no I/O.

A published spec is permanent, so everything that would make it unrunnable later is rejected
here: unknown fields, dangling edges, cycles, unreachable nodes, bad JMESPath, invalid JSON Schema
and primitive top-level state channels (ADR-0012).
"""

import hashlib
import re
from dataclasses import dataclass
from typing import Annotated, Any, Literal

import jmespath
import rfc8785
from jmespath.exceptions import JMESPathError
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

SPEC_SCHEMA_VERSION = 1
MAX_SPEC_BYTES = 256 * 1024
PROVIDERS = ("fake", "ollama", "anthropic", "openai")
RESERVED_STATE_KEYS = frozenset({"input"})  # filled with the execution input (always an object)

_IDENT = r"^[a-z][a-z0-9_]{0,63}$"
_PLACEHOLDER = re.compile(r"\{\{\s*(.*?)\s*\}\}", re.S)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Metadata(_Strict):
    description: str | None = Field(default=None, max_length=2000)
    tags: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(
        default_factory=list, max_length=20
    )


class Limits(_Strict):
    max_steps: int = Field(default=25, ge=1, le=200)


class ModelParams(_Strict):
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, gt=0, le=1)
    max_tokens: int | None = Field(default=None, ge=1, le=200_000)
    stop: list[Annotated[str, Field(min_length=1, max_length=64)]] | None = Field(
        default=None, max_length=4
    )


class ModelRef(_Strict):
    provider: Literal["fake", "ollama", "anthropic", "openai"]
    name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@-]*$")
    params: ModelParams = Field(default_factory=ModelParams)


class LlmConfig(_Strict):
    model: ModelRef
    system: str | None = Field(default=None, max_length=32_000)
    # `{{ <JMESPath over state> }}` placeholders, e.g. "Answer: {{ input.question }}".
    prompt: str = Field(min_length=1, max_length=32_000)
    # Top-level state channel the node writes: the output_schema object, else {"text": ...}.
    output_key: str = Field(pattern=_IDENT)
    output_schema: dict[str, Any] | None = None


class RouterConfig(_Strict):
    pass  # conditions live on the outgoing edges


class EndConfig(_Strict):
    # JMESPath over state selecting the execution output; omitted = the whole state.
    output: str | None = Field(default=None, min_length=1, max_length=1000)


class LlmNode(_Strict):
    id: str = Field(pattern=_IDENT)
    type: Literal["llm"]
    config: LlmConfig


class RouterNode(_Strict):
    id: str = Field(pattern=_IDENT)
    type: Literal["router"]
    config: RouterConfig = Field(default_factory=RouterConfig)


class EndNode(_Strict):
    id: str = Field(pattern=_IDENT)
    type: Literal["end"]
    config: EndConfig = Field(default_factory=EndConfig)


Node = Annotated[LlmNode | RouterNode | EndNode, Field(discriminator="type")]


class Edge(_Strict):
    from_: str = Field(alias="from")  # accepted and emitted only as "from"
    to: str
    # Router edges only. JMESPath over state; the first truthy edge wins, the one edge without
    # `when` is the default.
    when: str | None = Field(default=None, min_length=1, max_length=1000)


class SpecV1(_Strict):
    schema_version: Literal[1]
    metadata: Metadata = Field(default_factory=Metadata)
    entry: str
    state_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    limits: Limits = Field(default_factory=Limits)
    nodes: list[Node] = Field(min_length=1, max_length=50)
    edges: list[Edge] = Field(default_factory=list, max_length=200)


@dataclass(frozen=True)
class Issue:
    path: str
    code: str
    message: str


@dataclass(frozen=True)
class Result:
    spec: dict[str, Any] | None  # normalized (defaults filled), only when valid
    content_hash: str | None
    errors: list[Issue]

    @property
    def valid(self) -> bool:
        return not self.errors


def validate_spec(raw: Any) -> Result:
    if not isinstance(raw, dict):
        return Result(None, None, [Issue("", "not_an_object", "The spec must be a JSON object.")])
    try:
        spec = SpecV1.model_validate(raw)
    except ValidationError as e:
        return Result(None, None, [_from_pydantic(err) for err in e.errors()])
    errors = _check_graph(spec) + _check_expressions(spec) + _check_schemas(spec)
    if errors:
        return Result(None, None, errors)
    normalized = spec.model_dump(mode="json", by_alias=True)
    try:
        canonical = rfc8785.dumps(normalized)
    except rfc8785.CanonicalizationError as e:
        return Result(None, None, [Issue("", "not_canonicalizable", str(e))])
    if len(canonical) > MAX_SPEC_BYTES:
        return Result(None, None, [Issue("", "too_large", f"Spec exceeds {MAX_SPEC_BYTES} bytes.")])
    return Result(normalized, content_hash(canonical), [])


def content_hash(canonical: bytes) -> str:
    """sha256 over the RFC 8785 (JCS) bytes. The spec carries schema_version, so it is covered."""
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _from_pydantic(err: Any) -> Issue:
    path = ""
    for part in err["loc"]:
        path += f"[{part}]" if isinstance(part, int) else (f".{part}" if path else str(part))
    return Issue(path, err["type"], err["msg"])


def _check_graph(spec: SpecV1) -> list[Issue]:
    errors: list[Issue] = []
    nodes: dict[str, Node] = {}
    for i, node in enumerate(spec.nodes):
        if node.id in nodes:
            errors.append(Issue(f"nodes[{i}].id", "duplicate_node_id", f"Duplicate id {node.id}."))
        nodes[node.id] = node
    if spec.entry not in nodes:
        errors.append(Issue("entry", "unknown_node", f"Entry {spec.entry!r} is not a node."))

    out: dict[str, list[Edge]] = {n: [] for n in nodes}
    seen: set[tuple[str, str, str | None]] = set()
    for i, edge in enumerate(spec.edges):
        for end, name in (("from", edge.from_), ("to", edge.to)):
            if name not in nodes:
                errors.append(Issue(f"edges[{i}].{end}", "unknown_node", f"No node {name!r}."))
        if edge.from_ not in nodes or edge.to not in nodes:
            continue
        key = (edge.from_, edge.to, edge.when)
        if key in seen:
            errors.append(Issue(f"edges[{i}]", "duplicate_edge", "Duplicate edge."))
        seen.add(key)
        if edge.when is not None and nodes[edge.from_].type != "router":
            errors.append(Issue(f"edges[{i}].when", "when_not_allowed", "Only router edges."))
        out[edge.from_].append(edge)

    for i, node in enumerate(spec.nodes):
        edges, at = out.get(node.id, []), f"nodes[{i}]"
        if node.type == "end" and edges:
            errors.append(Issue(at, "end_has_edges", f"End node {node.id!r} has outgoing edges."))
        elif node.type == "llm" and len(edges) != 1:
            errors.append(Issue(at, "llm_out_degree", f"LLM node {node.id!r} needs one edge out."))
        elif node.type == "router" and sum(e.when is None for e in edges) != 1:
            errors.append(
                Issue(at, "router_default", f"Router {node.id!r} needs one default edge.")
            )
    if errors:
        return errors  # the walks below assume a well-formed edge set

    cycle = _find_cycle(out)
    if cycle:
        loop = " -> ".join(cycle)
        errors.append(Issue("edges", "cycle", f"Cycles are not supported in v1: {loop}."))
    reachable = _reach(spec.entry, out)
    for i, node in enumerate(spec.nodes):
        if node.id not in reachable:
            errors.append(Issue(f"nodes[{i}]", "unreachable", f"{node.id!r} is unreachable."))
    return errors


def _reach(start: str, out: dict[str, list[Edge]]) -> set[str]:
    seen, stack = {start}, [start]
    while stack:
        for edge in out[stack.pop()]:
            if edge.to not in seen:
                seen.add(edge.to)
                stack.append(edge.to)
    return seen


def _find_cycle(out: dict[str, list[Edge]]) -> list[str] | None:
    state: dict[str, int] = {}  # 1 = on the current path, 2 = done

    def visit(node: str, path: list[str]) -> list[str] | None:
        state[node] = 1
        for edge in out[node]:
            if state.get(edge.to) == 1:
                return [*path[path.index(edge.to) :], edge.to]
            if edge.to not in state and (found := visit(edge.to, [*path, edge.to])):
                return found
        state[node] = 2
        return None

    for node in out:
        if node not in state and (found := visit(node, [node])):
            return found
    return None


def _jmespath_issue(path: str, expr: str) -> Issue | None:
    try:
        jmespath.compile(expr)
    except JMESPathError as e:
        return Issue(path, "invalid_jmespath", f"Invalid JMESPath {expr!r}: {e}")
    return None


def _check_expressions(spec: SpecV1) -> list[Issue]:
    found: list[Issue | None] = []
    for i, edge in enumerate(spec.edges):
        if edge.when is not None:
            found.append(_jmespath_issue(f"edges[{i}].when", edge.when))
    for i, node in enumerate(spec.nodes):
        if isinstance(node, LlmNode):
            for field in ("system", "prompt"):
                for expr in _PLACEHOLDER.findall(getattr(node.config, field) or ""):
                    found.append(_jmespath_issue(f"nodes[{i}].config.{field}", expr))
        elif isinstance(node, EndNode) and node.config.output is not None:
            found.append(_jmespath_issue(f"nodes[{i}].config.output", node.config.output))
    return [issue for issue in found if issue is not None]


def _schema_issue(path: str, schema: dict[str, Any]) -> Issue | None:
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as e:
        return Issue(path, "invalid_json_schema", e.message)
    if schema.get("type") != "object":
        return Issue(path, "not_an_object_schema", 'Must have "type": "object".')
    return None


def _check_schemas(spec: SpecV1) -> list[Issue]:
    issue = _schema_issue("state_schema", spec.state_schema)
    if issue:
        return [issue]
    errors: list[Issue] = []
    channels: dict[str, Any] = spec.state_schema.get("properties") or {}
    # ADR-0012: top-level state channels must be containers (checkpoint encryption covers them).
    for name, sub in channels.items():
        kind = sub.get("type") if isinstance(sub, dict) else None
        allowed = ("object",) if name in RESERVED_STATE_KEYS else ("object", "array")
        if kind not in allowed:
            errors.append(
                Issue(
                    f"state_schema.properties.{name}",
                    "primitive_state_channel",
                    f"Top-level state channel {name!r} must have type {' or '.join(allowed)}.",
                )
            )
    for i, node in enumerate(spec.nodes):
        if not isinstance(node, LlmNode):
            continue
        at, key = f"nodes[{i}].config", node.config.output_key
        if key in RESERVED_STATE_KEYS:
            errors.append(Issue(f"{at}.output_key", "reserved_state_key", f"{key!r} is reserved."))
        declared = channels.get(key)
        if isinstance(declared, dict) and declared.get("type") != "object":
            errors.append(
                Issue(f"{at}.output_key", "output_key_type", f"Channel {key!r} must be an object.")
            )
        if node.config.output_schema is not None:
            issue = _schema_issue(f"{at}.output_schema", node.config.output_schema)
            if issue:
                errors.append(issue)
    return errors
