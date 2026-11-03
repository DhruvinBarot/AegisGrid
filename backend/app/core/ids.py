import uuid

import uuid_utils

from app.core.errors import NotFoundError


def new_id() -> uuid.UUID:
    # UUIDv7 is time-ordered (index-friendly); Python 3.13 has no uuid.uuid7.
    return uuid.UUID(bytes=uuid_utils.uuid7().bytes)


def encode_id(prefix: str, value: uuid.UUID) -> str:
    return f"{prefix}_{value.hex}"


def decode_id(prefix: str, public_id: str) -> uuid.UUID:
    """Inverse of `encode_id`. A wrong prefix or malformed id is a 404, never a 500."""
    head, sep, tail = public_id.partition("_")
    if head != prefix or not sep or len(tail) != 32:
        raise NotFoundError()
    try:
        return uuid.UUID(hex=tail)
    except ValueError:
        raise NotFoundError() from None
