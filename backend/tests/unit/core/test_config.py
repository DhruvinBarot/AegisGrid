import pytest
from pydantic import ValidationError

from app.core.config import Settings


def make(**kwargs: object) -> Settings:
    return Settings(_env_file=None, **kwargs)  # type: ignore[arg-type]


def test_production_refuses_dev_defaults() -> None:
    with pytest.raises(ValidationError, match="api_key_pepper"):
        make(env="production")


def test_invite_tokens_only_exposed_in_dev_and_test() -> None:
    assert make(env="test").expose_invite_tokens
    assert make(env="development").expose_invite_tokens


def test_production_with_real_secrets_starts() -> None:
    s = make(
        env="production",
        signing_key_passphrase="x" * 32,
        api_key_pepper="y" * 32,
        database_url="postgresql+psycopg://aegis_api:real@db:5432/aegisgrid",
        jwt_issuer="https://api.example.com",
        cookie_secure=True,
        allowed_origins=["https://app.example.com"],
    )
    assert not s.expose_invite_tokens


def test_production_refuses_localhost_origins() -> None:
    with pytest.raises(ValidationError, match="allowed_origins"):
        make(
            env="production",
            signing_key_passphrase="x" * 32,
            api_key_pepper="y" * 32,
            database_url="postgresql+psycopg://aegis_api:real@db:5432/aegisgrid",
            jwt_issuer="https://api.example.com",
            cookie_secure=True,
        )


def test_env_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AEGIS_ENV")
    with pytest.raises(ValidationError, match="env"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_staging_also_refuses_dev_defaults() -> None:
    with pytest.raises(ValidationError, match="staging refuses"):
        make(env="staging")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"access_token_ttl_seconds": 901},
        {"access_token_ttl_seconds": 0},
        {"refresh_idle_ttl_seconds": 100, "refresh_absolute_ttl_seconds": 50},
    ],
)
def test_ttl_bounds(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        make(env="test", **kwargs)


def test_database_url_is_masked() -> None:
    assert "aegis_api" not in repr(make(env="test"))
