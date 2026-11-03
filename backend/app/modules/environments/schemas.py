from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.core.ids import encode_id
from app.modules.environments.models import Environment

NAME = r"^[a-z0-9][a-z0-9_-]*$"


class EnvironmentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=NAME)
    kind: Literal["development", "staging", "production"]
    protected: bool = False
    settings: dict[str, Any] = Field(default_factory=dict)


class EnvironmentUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64, pattern=NAME)
    protected: bool | None = None
    settings: dict[str, Any] | None = None


class EnvironmentOut(BaseModel):
    id: str
    name: str
    kind: str
    protected: bool
    settings: dict[str, Any]
    created_at: datetime

    @classmethod
    def of(cls, e: Environment) -> "EnvironmentOut":
        return cls(
            id=encode_id("env", e.id),
            name=e.name,
            kind=e.kind,
            protected=e.protected,
            settings=e.settings,
            created_at=e.created_at,
        )
