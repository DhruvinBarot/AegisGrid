import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from httpx import AsyncClient

from app.core.config import settings
from tests.unit.identity.conftest import bearer, login, register


def claims_of(token: str) -> dict[str, Any]:
    return jwt.decode(token, options={"verify_signature": False})


def header_of(token: str) -> dict[str, Any]:
    return jwt.get_unverified_header(token)


def fresh_claims(token: str) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {**claims_of(token), "iat": now, "exp": now + timedelta(minutes=5)}


async def me_status(client: AsyncClient, token: str) -> int:
    return (await client.get("/v1/me", headers=bearer(token))).status_code


async def test_claims(session_tokens: dict[str, str]) -> None:
    token = session_tokens["access"]
    c = claims_of(token)
    assert {"iss", "aud", "sub", "sid", "jti", "iat", "exp"} <= c.keys()
    assert c["typ"] == "access"
    assert header_of(token)["alg"] == "ES256"
    assert c["exp"] - c["iat"] == settings.access_token_ttl_seconds


async def test_tampered(client: AsyncClient, session_tokens: dict[str, str]) -> None:
    head, payload, sig = session_tokens["access"].split(".")
    forged = json.dumps({**claims_of(session_tokens["access"]), "sid": "x"}).encode()
    body = base64.urlsafe_b64encode(forged).rstrip(b"=").decode()
    assert await me_status(client, f"{head}.{body}.{sig}") == 401
    flipped = sig[:-2] + ("AA" if sig[-2:] != "AA" else "BB")
    assert await me_status(client, f"{head}.{payload}.{flipped}") == 401


async def test_expired(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    await register(client)
    monkeypatch.setattr(settings, "access_token_ttl_seconds", -10)
    token = (await login(client)).json()["access_token"]
    monkeypatch.undo()
    assert await me_status(client, token) == 401


@pytest.mark.parametrize("field", ["jwt_audience", "jwt_issuer"])
async def test_wrong_aud_or_iss(
    client: AsyncClient,
    session_tokens: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    assert await me_status(client, session_tokens["access"]) == 200
    monkeypatch.setattr(settings, field, "other")
    assert await me_status(client, session_tokens["access"]) == 401


async def test_alg_none_and_hs256_rejected(
    client: AsyncClient, session_tokens: dict[str, str]
) -> None:
    good = session_tokens["access"]
    c = fresh_claims(good)
    h = {"kid": header_of(good)["kid"]}
    none = jwt.encode(c, None, algorithm="none", headers=h)  # pyright: ignore[reportArgumentType]
    assert await me_status(client, none) == 401
    assert await me_status(client, jwt.encode(c, "s" * 64, algorithm="HS256", headers=h)) == 401


async def test_unknown_kid_and_foreign_signature(
    client: AsyncClient, session_tokens: dict[str, str]
) -> None:
    good = session_tokens["access"]
    c = fresh_claims(good)
    other = ec.generate_private_key(ec.SECP256R1())
    unknown = jwt.encode(c, other, algorithm="ES256", headers={"kid": "nope"})
    known_kid = jwt.encode(c, other, algorithm="ES256", headers={"kid": header_of(good)["kid"]})
    assert await me_status(client, unknown) == 401
    assert await me_status(client, known_kid) == 401


async def test_jwks_shape(client: AsyncClient, session_tokens: dict[str, str]) -> None:
    r = await client.get("/.well-known/jwks.json")
    assert r.status_code == 200
    keys = r.json()["keys"]
    for k in keys:
        assert k["kty"] == "EC" and k["crv"] == "P-256" and k["alg"] == "ES256"
        assert k["use"] == "sig" and k["kid"]
        assert "d" not in k
    assert header_of(session_tokens["access"])["kid"] in {k["kid"] for k in keys}
