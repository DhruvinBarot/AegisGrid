from typing import Literal, Self

from pydantic import SecretStr, computed_field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Local-dev values only; the guard below refuses to start staging/production with any of them.
_DEV_PASSPHRASE = "dev-only-signing-key-passphrase"
_DEV_PEPPER = "dev-only-api-key-pepper"
_DEV_DATABASE_URL = "postgresql+psycopg://aegis_api:aegis_api@127.0.0.1:5433/aegisgrid"
_DEV_ISSUER = "aegisgrid-dev"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AEGIS_", env_file=("../.env", ".env"), extra="ignore"
    )

    # Required, no default: an unset AEGIS_ENV must fail loudly rather than fall back to dev.
    env: Literal["development", "test", "staging", "production"]
    # Defaults match docker-compose host port mappings, so host-side tooling works with no .env.
    # The app connects as aegis_api only. The migrator URL is read by migrations/env.py from
    # AEGIS_MIGRATOR_DATABASE_URL and is deliberately not part of the app's settings (ADR-0004).
    database_url: SecretStr = SecretStr(_DEV_DATABASE_URL)
    redis_url: str = "redis://127.0.0.1:6379/0"

    jwt_issuer: str = _DEV_ISSUER
    jwt_audience: str = "aegisgrid-api"
    access_token_ttl_seconds: int = 600
    refresh_idle_ttl_seconds: int = 604800
    refresh_absolute_ttl_seconds: int = 2592000
    signing_key_passphrase: SecretStr = SecretStr(_DEV_PASSPHRASE)
    api_key_pepper: SecretStr = SecretStr(_DEV_PEPPER)
    cookie_secure: bool = False
    # Browser origins allowed to call the cookie endpoints (checked only when Origin is present).
    allowed_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]
    rate_limit_window_seconds: int = 60
    login_ip_limit: int = 20
    login_email_limit: int = 5
    register_ip_limit: int = 10

    @computed_field  # type: ignore[prop-decorator]
    @property
    def expose_invite_tokens(self) -> bool:
        # No email delivery yet, so invite tokens are returned to the inviter in dev/test only.
        return self.env in {"development", "test"}

    @model_validator(mode="after")
    def _validate_ttls(self) -> Self:
        if not 0 < self.access_token_ttl_seconds <= 900:
            raise ValueError("access_token_ttl_seconds must be between 1 and 900")
        if not 0 < self.refresh_idle_ttl_seconds <= self.refresh_absolute_ttl_seconds:
            raise ValueError("refresh_idle_ttl_seconds must be positive and <= absolute ttl")
        return self

    @model_validator(mode="after")
    def _refuse_dev_defaults_outside_dev(self) -> Self:
        if self.env not in {"staging", "production"}:
            return self
        problems = [
            name
            for name, is_dev in {
                "signing_key_passphrase": self.signing_key_passphrase.get_secret_value()
                == _DEV_PASSPHRASE,
                "api_key_pepper": self.api_key_pepper.get_secret_value() == _DEV_PEPPER,
                "database_url": self.database_url.get_secret_value() == _DEV_DATABASE_URL,
                "jwt_issuer": self.jwt_issuer == _DEV_ISSUER,
                "cookie_secure": not self.cookie_secure,
                "allowed_origins": any(
                    "localhost" in o or "127.0.0.1" in o for o in self.allowed_origins
                ),
            }.items()
            if is_dev
        ]
        if problems:
            raise ValueError(f"{self.env} refuses dev defaults for: {', '.join(problems)}")
        return self


settings = Settings()  # pyright: ignore[reportCallIssue]
