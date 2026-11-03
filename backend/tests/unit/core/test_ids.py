import uuid

import pytest

from app.core.errors import NotFoundError
from app.core.ids import decode_id, encode_id, new_id


def test_new_id_is_uuid7_and_time_ordered() -> None:
    a, b = new_id(), new_id()
    assert a.version == 7
    assert a != b
    assert a.int >> 80 <= b.int >> 80  # 48-bit millisecond timestamp prefix


def test_round_trip() -> None:
    value = new_id()
    public = encode_id("org", value)
    assert public == f"org_{value.hex}"
    assert decode_id("org", public) == value


@pytest.mark.parametrize(
    "bad",
    [
        f"env_{uuid.uuid4().hex}",  # wrong prefix
        "org_not-hex",
        f"org_{uuid.uuid4().hex}00",  # too long
        f"org{uuid.uuid4().hex}",  # no separator
        f"org_{uuid.uuid4()}",  # hyphenated form is not canonical
        "",
        "org_",
    ],
)
def test_malformed_ids_are_not_found(bad: str) -> None:
    with pytest.raises(NotFoundError):
        decode_id("org", bad)
