import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import system_scope
from app.modules.identity import api_keys
from app.modules.identity.authn import authenticate_bearer
from app.modules.identity.models import Principal
from app.modules.organizations.models import ApiKey, Organization, ServiceAccount
from tests.unit.identity.conftest import bearer


async def make_key(
    db: AsyncSession, *, revoked: bool = False, ttl: timedelta = timedelta(days=1)
) -> tuple[str, uuid.UUID]:
    creator = Principal(kind="service")
    sa_principal = Principal(kind="service")
    org = Organization(slug=f"org-{uuid.uuid4().hex[:8]}", name="Org")
    db.add_all([creator, sa_principal, org])
    await db.flush()
    full, key_id, digest = api_keys.generate("test")
    with system_scope():
        sa = ServiceAccount(
            organization_id=org.id, principal_id=sa_principal.id, name="ci", created_by=creator.id
        )
        db.add(sa)
        await db.flush()
        db.add(
            ApiKey(
                organization_id=org.id,
                service_account_id=sa.id,
                environment_id=None,
                key_id=key_id,
                secret_hmac=digest,
                scopes=["agents:read"],
                expires_at=datetime.now(UTC) + ttl,
                revoked_at=datetime.now(UTC) if revoked else None,
                created_by=creator.id,
            )
        )
        await db.flush()
    return full, sa_principal.id


async def status(client: AsyncClient, key: str) -> int:
    return (await client.get("/v1/me", headers=bearer(key))).status_code


async def test_valid_key_authenticates_as_service(
    client: AsyncClient, db_session: AsyncSession
) -> None:

    key, principal_id = await make_key(db_session)

    principal = await authenticate_bearer(db_session, key)
    assert principal is not None
    assert principal.kind == "service" and principal.principal_id == principal_id
    assert principal.user_id is None and principal.session_id is None
    assert principal.api_key is not None and principal.api_key.scopes == {"agents:read"}
    # /me is a user-only endpoint: the key authenticates but is refused with 403, not 401.
    assert await status(client, key) == 403


async def test_wrong_secret_revoked_expired_malformed(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    key, _ = await make_key(db_session)
    assert await status(client, key[:-1] + ("a" if key[-1] != "a" else "b")) == 401
    revoked, _ = await make_key(db_session, revoked=True)
    assert await status(client, revoked) == 401
    expired, _ = await make_key(db_session, ttl=timedelta(seconds=-1))
    assert await status(client, expired) == 401
    unknown = api_keys.generate("live")[0]
    for bad in (
        unknown,
        "agk_",
        "agk_live_short_x",
        "agk_prod_" + "a" * 16 + "_x",
        "agk_live_" + "a" * 16,
    ):
        assert await status(client, bad) == 401


@pytest.mark.parametrize(
    "raw", ["agk_live_0123456789abcdef_se_cr-et", "agk_test_0123456789abcdef_x"]
)
def test_parse_keeps_underscores_in_secret(raw: str) -> None:
    parsed = api_keys.parse(raw)
    assert parsed is not None and parsed[0] == "0123456789abcdef"
    assert raw.endswith(parsed[1])
