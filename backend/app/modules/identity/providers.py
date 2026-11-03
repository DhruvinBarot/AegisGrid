"""Identity-provider boundary.

A provider only proves "this person controls this (issuer, subject)". Everything after that
(users, principals, memberships, sessions, access/refresh tokens) is issued by AegisGrid and does
not depend on the provider, so an OIDC provider can be added without changing that model.

`PasswordIdentityProvider` is MVP/local only. It is NOT the permanent enterprise solution: real
customers will sign in through their own IdP (OIDC/SAML) and this provider should then be disabled.
"""

import asyncio
import uuid
from dataclasses import dataclass
from typing import Protocol

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.models import UserIdentity


@dataclass(frozen=True)
class VerifiedIdentity:
    provider: str
    issuer: str
    subject: str
    email: str
    email_verified: bool


class IdentityProvider(Protocol):
    async def authenticate(
        self, session: AsyncSession, *, email: str, password: str
    ) -> VerifiedIdentity | None:
        """Return the verified identity, or None. Never says which part was wrong."""
        ...


def normalize_email(email: str) -> str:
    return email.strip().lower()


class PasswordIdentityProvider:
    name = "password"
    issuer = "aegisgrid"

    def __init__(self) -> None:
        self._hasher = PasswordHasher()  # argon2id with the library's current defaults
        # Verified against when the email is unknown, so both paths cost one argon2 verification.
        self._dummy_hash = self._hasher.hash("aegisgrid-timing-equalizer")

    async def hash_password(self, password: str) -> str:
        return await asyncio.to_thread(self._hasher.hash, password)

    async def add_identity(
        self, session: AsyncSession, *, user_id: uuid.UUID, email: str, password: str
    ) -> UserIdentity:
        identity = UserIdentity(
            user_id=user_id,
            provider=self.name,
            issuer=self.issuer,
            subject=str(user_id),
            email=normalize_email(email),
            email_verified=False,
            password_hash=await self.hash_password(password),
        )
        session.add(identity)
        return identity

    async def authenticate(
        self, session: AsyncSession, *, email: str, password: str
    ) -> VerifiedIdentity | None:
        identity = (
            await session.execute(
                select(UserIdentity).where(
                    UserIdentity.provider == self.name,
                    UserIdentity.issuer == self.issuer,
                    UserIdentity.email == normalize_email(email),
                )
            )
        ).scalar_one_or_none()
        stored = identity.password_hash if identity else None
        try:
            await asyncio.to_thread(self._hasher.verify, stored or self._dummy_hash, password)
        except (VerificationError, InvalidHashError):
            return None
        if identity is None or stored is None:
            return None
        return VerifiedIdentity(
            provider=identity.provider,
            issuer=identity.issuer,
            subject=identity.subject,
            email=identity.email,
            email_verified=identity.email_verified,
        )


password_provider = PasswordIdentityProvider()
