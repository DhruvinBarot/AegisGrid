from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.errors import ConflictError, NotFoundError, register_error_handlers
from app.core.ids import decode_id


@pytest.fixture
async def problem_client() -> AsyncIterator[AsyncClient]:
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/missing")
    async def missing() -> None:
        raise NotFoundError("No such thing.")

    @app.get("/conflict")
    async def conflict() -> None:
        raise ConflictError("Taken.", code="slug_taken", headers={"X-Test": "1"})

    @app.get("/items/{item_id}")
    async def item(item_id: int) -> dict[str, int]:
        return {"id": item_id}

    @app.get("/orgs/{public_id}")
    async def org(public_id: str) -> None:
        decode_id("org", public_id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def test_problem_error_shape(problem_client: AsyncClient) -> None:
    resp = await problem_client.get("/missing")
    assert resp.status_code == 404
    assert resp.headers["content-type"] == "application/problem+json"
    assert resp.json() == {
        "type": "about:blank",
        "title": "Not Found",
        "status": 404,
        "detail": "No such thing.",
        "instance": "/missing",
    }


async def test_code_and_headers(problem_client: AsyncClient) -> None:
    resp = await problem_client.get("/conflict")
    assert resp.status_code == 409
    assert resp.json()["code"] == "slug_taken"
    assert resp.headers["x-test"] == "1"


async def test_unknown_route_is_problem_json(problem_client: AsyncClient) -> None:
    resp = await problem_client.get("/nope")
    assert resp.status_code == 404
    assert resp.headers["content-type"] == "application/problem+json"
    assert resp.json()["title"] == "Not Found"


async def test_validation_error_is_422_problem(problem_client: AsyncClient) -> None:
    resp = await problem_client.get("/items/abc")
    assert resp.status_code == 422
    body = resp.json()
    assert resp.headers["content-type"] == "application/problem+json"
    assert body["status"] == 422
    assert body["errors"][0]["loc"] == ["path", "item_id"]


async def test_bad_public_id_is_404_not_500(problem_client: AsyncClient) -> None:
    resp = await problem_client.get("/orgs/garbage")
    assert resp.status_code == 404
