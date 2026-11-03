"""Access tokens (ADR-0005): ES256 JWTs signed with keys stored in `signing_keys`.

Claims: iss, aud, sub (the principal's UUID), sid (session UUID), jti, iat, exp, typ="access".
Private keys are stored as PEM encrypted with `settings.signing_key_passphrase` (KMS comes later).
"""

import json
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.ids import new_id
from app.modules.identity.models import SigningKey

log = logging.getLogger(__name__)

ALG = "ES256"
_PUBLISHED = ("next", "active", "retiring")
_private_keys: dict[str, ec.EllipticCurvePrivateKey] = {}  # decrypted once per kid


def _passphrase() -> bytes:
    return settings.signing_key_passphrase.get_secret_value().encode()


async def _generate_key(session: AsyncSession) -> SigningKey:
    private = ec.generate_private_key(ec.SECP256R1())
    kid = secrets.token_hex(8)
    jwk: dict[str, Any] = json.loads(ECAlgorithm.to_jwk(private.public_key()))
    jwk.update(kid=kid, use="sig", alg=ALG)
    row = SigningKey(
        id=new_id(),
        kid=kid,
        alg=ALG,
        public_jwk=jwk,
        private_key_pem_encrypted=private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(_passphrase()),
        ),
        status="active",
        not_before=datetime.now(UTC),
    )
    session.add(row)
    await session.flush()
    _private_keys[kid] = private
    if settings.env in {"staging", "production"}:
        log.warning("generated signing key", extra={"event": "signing_key_generated", "kid": kid})
    return row


async def _active_key(session: AsyncSession) -> SigningKey:
    row = (
        await session.execute(
            select(SigningKey)
            .where(SigningKey.status == "active")
            .order_by(SigningKey.not_before.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    # shortcut: two concurrent first requests may each create a key; harmless, rotate later.
    return row or await _generate_key(session)


def _private_key(row: SigningKey) -> ec.EllipticCurvePrivateKey:
    key = _private_keys.get(row.kid)
    if key is None:
        loaded = serialization.load_pem_private_key(row.private_key_pem_encrypted, _passphrase())
        assert isinstance(loaded, ec.EllipticCurvePrivateKey)
        key = _private_keys[row.kid] = loaded
    return key


async def issue_access_token(
    session: AsyncSession, *, principal_id: uuid.UUID, session_id: uuid.UUID
) -> tuple[str, int]:
    """Return (token, expires_in seconds)."""
    row = await _active_key(session)
    now = datetime.now(UTC)
    ttl = settings.access_token_ttl_seconds
    claims = {
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "sub": str(principal_id),
        "sid": str(session_id),
        "jti": uuid.uuid4().hex,
        "iat": now,
        "exp": now + timedelta(seconds=ttl),
        "typ": "access",
    }
    token = jwt.encode(claims, _private_key(row), algorithm=ALG, headers={"kid": row.kid})
    return token, ttl


async def verify_access_token(session: AsyncSession, token: str) -> dict[str, Any] | None:
    """Return the claims of a valid access token, else None. Never raises on bad input."""
    try:
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        if header.get("alg") != ALG or not isinstance(kid, str):
            return None
        jwk = (
            await session.execute(
                select(SigningKey.public_jwk).where(
                    SigningKey.kid == kid, SigningKey.status.in_(_PUBLISHED)
                )
            )
        ).scalar_one_or_none()
        if jwk is None:
            return None
        claims = jwt.decode(
            token,
            jwt.PyJWK.from_dict(jwk).key,
            algorithms=[ALG],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            options={"require": ["exp", "iat", "sub", "sid", "jti"]},
        )
    except (jwt.PyJWTError, ValueError, TypeError):
        return None
    return claims if claims.get("typ") == "access" else None


async def jwks(session: AsyncSession) -> dict[str, list[dict[str, Any]]]:
    await _active_key(session)  # ensure a first key exists so the set is never empty
    rows = await session.scalars(select(SigningKey).where(SigningKey.status.in_(_PUBLISHED)))
    return {"keys": [r.public_jwk for r in rows]}
