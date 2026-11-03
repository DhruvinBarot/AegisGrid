from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.core import db
from app.core.cache import redis
from app.core.errors import register_error_handlers
from app.modules.agents.router import router as agents_router
from app.modules.audit.router import router as audit_router
from app.modules.environments.router import router as environments_router
from app.modules.identity.router import router as identity_router
from app.modules.organizations.router import router as organizations_router


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    await redis.aclose()
    await db.engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(title="AegisGrid", version="0.1.0", lifespan=lifespan)
    register_error_handlers(app)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        checks: dict[str, str] = {}
        try:
            async with db.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            checks["postgres"] = "ok"
        except Exception:
            checks["postgres"] = "unavailable"
        try:
            await redis.ping()
            checks["redis"] = "ok"
        except Exception:
            checks["redis"] = "unavailable"
        # Only Postgres gates readiness: the API must keep serving when Redis is down.
        ready = checks["postgres"] == "ok"
        return JSONResponse(
            {"status": "ready" if ready else "not_ready", "checks": checks},
            status_code=200 if ready else 503,
        )

    for router in (
        identity_router,
        organizations_router,
        environments_router,
        agents_router,
        audit_router,
    ):
        app.include_router(router)

    return app


app = create_app()
