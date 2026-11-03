"""Access-token security against real tokens from /v1/auth/login (ADR-0005)."""

import base64
import hashlib
import hmac
import json
import time
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from tests.integration.helpers import CSRF, Actor, Api


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _json_b64(obj: dict[str, Any]) -> str:
    return _b64(json.dumps(obj, separators=(",", ":")).encode())


class Forge:
    """Mints tokens around a real session, using the server's own active signing key, so each
    negative case differs from a valid token in exactly one respect."""

    def __init__(self, kid: str, pem: bytes, real: str) -> None:
        self.kid = kid
        self.private: Any = serialization.load_pem_private_key(
            pem, settings.signing_key_passphrase.get_secret_value().encode()
        )
        c = jwt.decode(real, options={"verify_signature": False})
        self.base: dict[str, Any] = {
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
            "sub": c["sub"],
            "sid": c["sid"],
            "jti": "forged",
            "iat": int(time.time()),
            "exp": int(time.time()) + 300,
            "typ": "access",
        }

    def es256(self, headers: dict[str, Any] | None = None, **claims: Any) -> str:
        payload = {k: v for k, v in {**self.base, **claims}.items() if v is not None}
        return jwt.encode(
            payload, self.private, algorithm="ES256", headers={"kid": self.kid, **(headers or {})}
        )


@pytest.fixture
async def user(api: Api) -> Actor:
    return await api.actor("jwt@example.com")


@pytest.fixture
async def forge(db_session: AsyncSession, user: Actor) -> Forge:
    row = (
        await db_session.execute(
            text(
                "SELECT kid, private_key_pem_encrypted FROM signing_keys "
                "WHERE status = 'active' ORDER BY not_before DESC LIMIT 1"
            )
        )
    ).one()
    return Forge(row.kid, bytes(row.private_key_pem_encrypted), user.access)


async def status_of(client: AsyncClient, token: str) -> int:
    r = await client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
    return r.status_code


async def test_control_forged_token_with_valid_claims_is_accepted(
    client: AsyncClient, forge: Forge
) -> None:
    # Guards the negative cases below: the forge itself must produce acceptable tokens.
    assert await status_of(client, forge.es256()) == 200


async def test_real_token_works_and_401_is_problem_json(client: AsyncClient, user: Actor) -> None:
    assert await status_of(client, user.access) == 200
    r = await client.get("/v1/me")
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.headers["www-authenticate"] == "Bearer"


async def test_tampered_signature_rejected(client: AsyncClient, user: Actor) -> None:
    head, payload, sig = user.access.split(".")
    mid = len(sig) // 2
    flipped = sig[:mid] + ("A" if sig[mid] != "A" else "B") + sig[mid + 1 :]
    assert await status_of(client, f"{head}.{payload}.{flipped}") == 401


async def test_tampered_payload_rejected(client: AsyncClient, user: Actor) -> None:
    head, payload, sig = user.access.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
    claims["exp"] += 100000
    assert await status_of(client, f"{head}.{_json_b64(claims)}.{sig}") == 401


async def test_alg_none_rejected(client: AsyncClient, forge: Forge) -> None:
    header = _json_b64({"alg": "none", "typ": "JWT", "kid": forge.kid})
    assert await status_of(client, f"{header}.{_json_b64(forge.base)}.") == 401


async def test_hs256_signed_with_public_key_rejected(client: AsyncClient, forge: Forge) -> None:
    jwks = (await client.get("/.well-known/jwks.json")).json()["keys"]
    jwk = next(k for k in jwks if k["kid"] == forge.kid)
    public: Any = jwt.PyJWK.from_dict(jwk).key
    public_pem = public.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    header = _json_b64({"alg": "HS256", "typ": "JWT", "kid": forge.kid})
    signing_input = f"{header}.{_json_b64(forge.base)}"
    sig = hmac.new(public_pem, signing_input.encode(), hashlib.sha256).digest()
    assert await status_of(client, f"{signing_input}.{_b64(sig)}") == 401


@pytest.mark.parametrize(
    "claims",
    [
        {"aud": "someone-else"},
        {"iss": "https://evil.example"},
        {"exp": int(time.time()) - 60},
        {"typ": "refresh"},
        {"typ": None},
        {"sid": None},
        {"sub": None},
        {"exp": None},
    ],
    ids=[
        "wrong-aud",
        "wrong-iss",
        "expired",
        "typ-refresh",
        "typ-missing",
        "no-sid",
        "no-sub",
        "no-exp",
    ],
)
async def test_validly_signed_but_bad_claims_rejected(
    client: AsyncClient, forge: Forge, claims: dict[str, Any]
) -> None:
    assert await status_of(client, forge.es256(**claims)) == 401


async def test_unknown_kid_rejected(client: AsyncClient, forge: Forge) -> None:
    assert await status_of(client, forge.es256(headers={"kid": "ffffffffffffffff"})) == 401


async def test_missing_kid_rejected(client: AsyncClient, forge: Forge) -> None:
    token = jwt.encode(forge.base, forge.private, algorithm="ES256")
    assert await status_of(client, token) == 401


async def test_valid_token_for_unknown_session_rejected(client: AsyncClient, forge: Forge) -> None:
    assert await status_of(client, forge.es256(sid="00000000-0000-0000-0000-000000000000")) == 401


async def test_garbage_bearer_values_rejected(client: AsyncClient) -> None:
    for value in (
        "Bearer",
        "Bearer ",
        "Bearer a.b.c",
        "Basic abc",
        "bearer x",
        "Bearer agk_live_x",
    ):
        r = await client.get("/v1/me", headers={"Authorization": value})
        assert r.status_code == 401, value


async def test_logout_revokes_that_session_only(client: AsyncClient, api: Api, user: Actor) -> None:
    other = await api.login(user.email)
    r = await client.post("/v1/auth/logout", headers={**user.h, **CSRF})
    assert r.status_code == 204
    assert await status_of(client, user.access) == 401
    assert await status_of(client, other.access) == 200
    # the logged-out session's refresh token is dead too
    r = await client.post(
        "/v1/auth/refresh", headers={**CSRF, "Cookie": f"ag_refresh={user.refresh}"}
    )
    assert r.status_code == 401


async def test_logout_all_revokes_every_session(client: AsyncClient, api: Api, user: Actor) -> None:
    others = [await api.login(user.email) for _ in range(2)]
    r = await client.post("/v1/auth/logout-all", headers={**user.h, **CSRF})
    assert r.status_code == 204
    for a in (user, *others):
        assert await status_of(client, a.access) == 401
        r = await client.post(
            "/v1/auth/refresh", headers={**CSRF, "Cookie": f"ag_refresh={a.refresh}"}
        )
        assert r.status_code == 401
    fresh = await api.login(user.email)  # logging in again still works
    assert await status_of(client, fresh.access) == 200


async def test_logout_requires_csrf_header(client: AsyncClient, user: Actor) -> None:
    r = await client.post("/v1/auth/logout", headers=user.h)
    assert r.status_code == 403
    assert await status_of(client, user.access) == 200


async def test_jwks_publishes_only_public_material(client: AsyncClient, user: Actor) -> None:
    r = await client.get("/.well-known/jwks.json")
    assert r.status_code == 200
    keys = r.json()["keys"]
    assert keys
    for k in keys:
        assert k["kty"] == "EC" and k["crv"] == "P-256"
        assert k["alg"] == "ES256" and k["use"] == "sig" and k["kid"]
        assert not {"d", "p", "q", "dp", "dq", "qi", "k"} & k.keys()
    assert "PRIVATE" not in r.text
    kid = jwt.get_unverified_header(user.access)["kid"]
    jwk = next(k for k in keys if k["kid"] == kid)  # the published key verifies a real token
    jwt.decode(
        user.access,
        jwt.PyJWK.from_dict(jwk).key,
        algorithms=["ES256"],
        audience=settings.jwt_audience,
        issuer=settings.jwt_issuer,
    )


async def test_login_failures_are_indistinguishable(client: AsyncClient, api: Api) -> None:
    await api.actor("real@example.com")
    bodies = []
    for email, pw in (
        ("real@example.com", "wrong password!!"),
        ("ghost@example.com", "whatever12345"),
    ):
        r = await client.post("/v1/auth/login", json={"email": email, "password": pw}, headers=CSRF)
        assert r.status_code == 401
        assert "set-cookie" not in r.headers
        bodies.append({k: v for k, v in r.json().items() if k != "instance"})
    assert bodies[0] == bodies[1]


async def test_login_requires_csrf_header(client: AsyncClient, api: Api) -> None:
    await api.actor("csrf@example.com")
    r = await client.post(
        "/v1/auth/login", json={"email": "csrf@example.com", "password": "correct horse battery"}
    )
    assert r.status_code == 403
