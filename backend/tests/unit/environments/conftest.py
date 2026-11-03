import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.unit.organizations.support import World


@pytest.fixture
def world(app: FastAPI, client: AsyncClient, db_session: AsyncSession) -> World:
    return World(app, client, db_session)
