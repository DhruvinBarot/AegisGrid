"""Control-plane modules must not depend on execution-plane modules (ADR-0001).

Execution-plane modules do not exist yet; the rule is written against names, so it starts biting
the moment `app/modules/runtime` etc. appear.
"""

import ast
from pathlib import Path

import pytest

MODULES = Path(__file__).resolve().parents[2] / "app" / "modules"
CONTROL_PLANE = ("identity", "organizations", "environments", "audit", "agents")
EXECUTION_PLANE = ("runtime", "gateway", "memory", "executions")


def forbidden_imports(source: str, package: str) -> list[str]:
    """Names of execution-plane modules imported anywhere in `source` (also lazy imports)."""
    hits: list[str] = []
    for node in ast.walk(ast.parse(source)):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:  # relative: resolve against app.modules.<package>
                parts = ["app", "modules", package]
                parts = parts[: len(parts) - (node.level - 1)]
                base = ".".join([*parts, *([base] if base else [])])
            names = [base, *[f"{base}.{a.name}" for a in node.names]]
        for name in names:
            parts = name.split(".")
            if parts[:2] == ["app", "modules"] and len(parts) > 2 and parts[2] in EXECUTION_PLANE:
                hits.append(name)
    return hits


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import app.modules.runtime.service", True),
        ("from app.modules.gateway import x", True),
        ("from app.modules import memory", True),
        ("def f():\n    from app.modules.executions.models import E", True),
        ("from ..runtime import x", True),
        ("from app.modules.identity import x\nimport os", False),
        ("from app.core import tenancy", False),
    ],
)
def test_detector_flags_what_it_should(source: str, expected: bool) -> None:
    assert bool(forbidden_imports(source, "organizations")) is expected


def test_control_plane_does_not_import_execution_plane() -> None:
    checked = 0
    violations: list[str] = []
    for package in CONTROL_PLANE:
        root = MODULES / package
        assert root.is_dir(), f"control-plane module {package} missing; update CONTROL_PLANE"
        for path in root.rglob("*.py"):
            checked += 1
            for name in forbidden_imports(path.read_text(encoding="utf-8"), package):
                violations.append(f"{path.relative_to(MODULES.parent.parent)} imports {name}")
    assert checked > 20, "scan looks empty"
    assert not violations, "\n".join(violations)
