"""Mandatory CI gate: tenant isolation as seen through every org-scoped route.

Routes are discovered from `app.routes`, so a newly added `{org}` route is covered automatically.
A caller who is not a member of the addressed org (or addresses another org's resource) must get
404, never 403/200/422, so an org's existence and contents are not leaked.
"""

import re
from collections.abc import Callable, Iterable
from typing import Any

from app.main import app
from tests.integration.helpers import Api, Populated, future, populate

_PARAM = re.compile(r"\{(\w+)\}")


def org_routes() -> list[tuple[str, str]]:
    """(method, path) of every route with {org}. FastAPI nests included routers lazily, so read the
    flattened view when there is one, and the OpenAPI schema as a second source."""
    found: set[tuple[str, str]] = set()
    pending: list[Any] = list(app.routes)
    while pending:
        route = pending.pop()
        flat: Callable[[], Iterable[Any]] | None = getattr(route, "effective_route_contexts", None)
        if callable(flat):
            pending.extend(flat())
        elif hasattr(route, "path") and hasattr(route, "methods"):
            found.update((m, route.path) for m in route.methods)
    for path, ops in app.openapi()["paths"].items():
        found.update((m.upper(), path) for m in ops)
    result = sorted((m, p) for m, p in found if "{org}" in p and m not in {"HEAD", "OPTIONS"})
    assert len(result) >= 10, f"route discovery looks broken: {result}"
    return result


# Minimal valid bodies. Unlisted routes get {} (only matters for POST/PATCH/PUT).
BODIES: dict[tuple[str, str], dict[str, Any]] = {
    ("PATCH", "/v1/orgs/{org}"): {"name": "renamed"},
    ("PATCH", "/v1/orgs/{org}/members/{member}"): {"role": "viewer"},
    ("POST", "/v1/orgs/{org}/invitations"): {"email": "x@example.com", "role": "viewer"},
    ("POST", "/v1/orgs/{org}/environments"): {"name": "new-env", "kind": "development"},
    ("PATCH", "/v1/orgs/{org}/environments/{env}"): {"name": "renamed-env"},
    ("POST", "/v1/orgs/{org}/service-accounts"): {"name": "bot2", "role": "viewer"},
    ("POST", "/v1/orgs/{org}/agents"): {"name": "New", "slug": "new-agent"},
    ("PATCH", "/v1/orgs/{org}/agents/{agent}"): {"name": "renamed"},
    ("PUT", "/v1/orgs/{org}/agents/{agent}/draft"): {"spec": {}, "draft_rev": 1},
    ("POST", "/v1/orgs/{org}/agents/{agent}/versions"): {},
    ("POST", "/v1/orgs/{org}/environments/{env}/deployments"): {"version_id": "ver_" + "0" * 32},
    ("POST", "/v1/orgs/{org}/deployments/{deployment}/rollback"): {},
    ("POST", "/v1/orgs/{org}/service-accounts/{sa}/keys"): {
        "scopes": ["org:read"],
        "expires_at": future(),
    },
}


def _url(path: str, ids: dict[str, str]) -> str:
    unknown = set(_PARAM.findall(path)) - ids.keys()
    assert not unknown, f"{path}: meta-test has no resource id for {unknown}; add it to populate()"
    return path.format(**ids)


async def _call(api: Api, method: str, path: str, ids: dict[str, str], headers: dict[str, str]):
    body = BODIES.get((method, path))
    return await api.c.request(method, _url(path, ids), headers=headers, json=body)


async def _two_orgs(api: Api) -> tuple[Populated, Populated]:
    return await populate(api, "a"), await populate(api, "b")


async def test_every_org_route_is_404_across_orgs(api: Api) -> None:
    a, b = await _two_orgs(api)
    bad: list[str] = []
    for method, path in org_routes():
        params = set(_PARAM.findall(path))
        cases = {
            "B org + B resources": b.ids,
            "B org + A resources": {**a.ids, "org": b.org},
        }
        if params - {"org"}:  # with only {org}, A's org + A's id is a legitimate request
            cases["A org + B resources"] = {**b.ids, "org": a.org}
        for label, ids in cases.items():
            r = await _call(api, method, path, ids, a.owner.h)
            if r.status_code != 404:
                bad.append(f"{method} {path} [{label}] -> {r.status_code}")
    assert not bad, "non-404 across orgs:\n" + "\n".join(bad)


async def test_every_org_route_requires_authentication(api: Api) -> None:
    a = await populate(api, "a")
    bad = []
    for method, path in org_routes():
        r = await _call(api, method, path, a.ids, {})
        if r.status_code != 401:
            bad.append(f"{method} {path} -> {r.status_code}")
    assert not bad, "unauthenticated requests not rejected with 401:\n" + "\n".join(bad)


async def test_malformed_or_wrong_prefix_ids_are_404(api: Api) -> None:
    a = await populate(api, "a")
    bad: list[str] = []
    for method, path in org_routes():
        for param in set(_PARAM.findall(path)):
            prefix, _, hexpart = a.ids[param].partition("_")
            other = next(v for k, v in a.ids.items() if k != param and v.split("_")[0] != prefix)
            variants = [
                "nope",
                other,  # a real id with the wrong prefix
                f"{prefix}_{'z' * 32}",
                f"{prefix}_{hexpart[:-1]}",
                f"{prefix}_{hexpart}00",
                hexpart,  # no prefix
            ]
            for bad_id in variants:
                r = await _call(api, method, path, {**a.ids, param: bad_id}, a.owner.h)
                if r.status_code != 404:
                    bad.append(f"{method} {path} [{param}={bad_id}] -> {r.status_code}")
    assert not bad, "malformed ids not answered with 404:\n" + "\n".join(bad)
