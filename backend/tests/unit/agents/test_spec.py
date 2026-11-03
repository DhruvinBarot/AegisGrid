"""Spec schema v1 validation and content hashing (ADR-0021). Pure functions, no database."""

import copy
import hashlib
from typing import Any

import pytest
import rfc8785

from app.modules.agents.spec import validate_spec
from tests.integration.helpers import SPEC, spec

# Pinned: if this changes, every published content_hash would silently stop matching its spec.
GOLDEN = "sha256:87173233036f01cbdde6cd984b5d0b2834eee95e1774447a887dd503ea884044"

ROUTED: dict[str, Any] = {
    "schema_version": 1,
    "entry": "classify",
    "state_schema": {
        "type": "object",
        "properties": {"input": {"type": "object"}, "triage": {"type": "object"}},
    },
    "nodes": [
        {
            "id": "classify",
            "type": "llm",
            "config": {
                "model": {
                    "provider": "ollama",
                    "name": "llama3.2:3b",
                    "params": {"temperature": 0},
                },
                "system": "You triage tickets.",
                "prompt": "Ticket: {{ input.body }}",
                "output_key": "triage",
                "output_schema": {
                    "type": "object",
                    "properties": {"urgent": {"type": "boolean"}},
                    "required": ["urgent"],
                },
            },
        },
        {"id": "route", "type": "router"},
        {"id": "escalate", "type": "end", "config": {"output": "triage"}},
        {"id": "done", "type": "end"},
    ],
    "edges": [
        {"from": "classify", "to": "route"},
        {"from": "route", "to": "escalate", "when": "triage.urgent == `true`"},
        {"from": "route", "to": "done"},
    ],
}


def codes(raw: Any) -> set[str]:
    return {e.code for e in validate_spec(raw).errors}


def test_valid_specs_pass_and_normalize() -> None:
    for raw in (SPEC, ROUTED):
        r = validate_spec(raw)
        assert r.valid, r.errors
        assert r.spec is not None and r.spec["limits"] == {"max_steps": 25}
        assert r.spec["metadata"] == {"description": None, "tags": []}


def test_hash_is_jcs_sha256_and_stable() -> None:
    r = validate_spec(SPEC)
    assert r.spec is not None
    assert r.content_hash == "sha256:" + hashlib.sha256(rfc8785.dumps(r.spec)).hexdigest()
    assert r.content_hash == GOLDEN
    reordered = {k: SPEC[k] for k in reversed(list(SPEC))}
    assert validate_spec(reordered).content_hash == GOLDEN
    assert validate_spec(spec(limits={"max_steps": 25})).content_hash == GOLDEN  # explicit default
    assert validate_spec(spec(limits={"max_steps": 24})).content_hash != GOLDEN


def _mut(base: dict[str, Any], path: list[Any], value: Any) -> dict[str, Any]:
    out = copy.deepcopy(base)
    target = out
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return out


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ([], "not_an_object"),
        (spec(schema_version=2), "literal_error"),
        (spec(extra_field=1), "extra_forbidden"),
        (spec(nodes=[]), "too_short"),
        (spec(entry="missing"), "unknown_node"),
        (_mut(SPEC, ["nodes", 0, "type"], "tool"), "union_tag_invalid"),
        (_mut(SPEC, ["nodes", 0, "id"], "Bad Id"), "string_pattern_mismatch"),
        (_mut(SPEC, ["nodes", 1, "id"], "answer"), "duplicate_node_id"),
        (_mut(SPEC, ["nodes", 0, "config", "model", "provider"], "skynet"), "literal_error"),
        (
            _mut(SPEC, ["nodes", 0, "config", "model", "params"], {"temperature": 3}),
            "less_than_equal",
        ),
        (_mut(SPEC, ["nodes", 0, "config", "model", "params"], {"seed": 1}), "extra_forbidden"),
        (_mut(SPEC, ["nodes", 0, "config", "model", "name"], ""), "string_too_short"),
        (_mut(SPEC, ["nodes", 0, "config", "prompt"], "{{ [ }}"), "invalid_jmespath"),
        (_mut(SPEC, ["nodes", 0, "config", "output_key"], "input"), "reserved_state_key"),
        (
            _mut(SPEC, ["nodes", 0, "config", "output_schema"], {"type": "string"}),
            "not_an_object_schema",
        ),
        (_mut(SPEC, ["nodes", 0, "config", "output_schema"], {"type": 5}), "invalid_json_schema"),
        (_mut(SPEC, ["nodes", 1, "config", "output"], "a[?"), "invalid_jmespath"),
        (spec(edges=[]), "llm_out_degree"),
        (spec(edges=[{"from": "answer", "to": "ghost"}]), "unknown_node"),
        (spec(edges=[*SPEC["edges"], {"from": "done", "to": "answer"}]), "end_has_edges"),
        (spec(edges=[{"from": "answer", "to": "done", "when": "x"}]), "when_not_allowed"),
        (spec(edges=[*SPEC["edges"], *SPEC["edges"]]), "duplicate_edge"),
        (spec(nodes=[*SPEC["nodes"], {"id": "orphan", "type": "end"}]), "unreachable"),
        (
            spec(state_schema={"type": "object", "properties": {"answer": {"type": "string"}}}),
            "primitive_state_channel",
        ),
        (spec(state_schema={"type": "array"}), "not_an_object_schema"),
        (spec(limits={"max_steps": 0}), "greater_than_equal"),
        (_mut(ROUTED, ["edges", 2, "when"], "triage.other"), "router_default"),
        (_mut(ROUTED, ["edges", 1, "when"], "triage.["), "invalid_jmespath"),
        (
            _mut(
                SPEC, ["nodes", 0, "config", "output_schema"], {"type": "object", "maximum": 2**60}
            ),
            "not_canonicalizable",
        ),
    ],
)
def test_invalid_specs_are_rejected(raw: Any, code: str) -> None:
    assert code in codes(raw)


def test_cycles_are_rejected_and_named() -> None:
    retry = {"from": "route", "to": "classify", "when": "triage.retry"}
    looped = {**ROUTED, "edges": [*ROUTED["edges"], retry]}
    errors = validate_spec(looped).errors
    assert [e.code for e in errors] == ["cycle"]
    assert "classify -> route -> classify" in errors[0].message


def test_error_paths_point_at_the_field() -> None:
    raw = _mut(SPEC, ["nodes", 0, "config", "model", "provider"], "skynet")
    (err,) = validate_spec(raw).errors
    assert err.path == "nodes[0].llm.config.model.provider"


def test_strict_types_do_not_coerce() -> None:
    raw = _mut(SPEC, ["nodes", 0, "config", "model", "params"], {"max_tokens": "100"})
    assert "int_type" in codes(raw)
