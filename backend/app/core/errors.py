from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DataError
from starlette.exceptions import HTTPException as StarletteHTTPException

PROBLEM_JSON = "application/problem+json"


class ProblemError(Exception):
    """RFC 9457 problem details. Subclass or raise directly from request handlers."""

    status = 500
    title = "Internal Server Error"

    def __init__(
        self,
        detail: str | None = None,
        *,
        status: int | None = None,
        title: str | None = None,
        code: str | None = None,
        headers: dict[str, str] | None = None,
        **extra: Any,
    ) -> None:
        super().__init__(detail or title or self.title)
        self.detail = detail
        self.status = status or self.status
        self.title = title or self.title
        self.code = code
        self.headers = headers
        self.extra = extra


class NotFoundError(ProblemError):
    status = 404
    title = "Not Found"


class UnauthorizedError(ProblemError):
    status = 401
    title = "Unauthorized"


class ForbiddenError(ProblemError):
    status = 403
    title = "Forbidden"


class ConflictError(ProblemError):
    status = 409
    title = "Conflict"


def _problem(
    request: Request,
    status: int,
    title: str,
    detail: str | None = None,
    code: str | None = None,
    headers: dict[str, str] | None = None,
    **extra: Any,
) -> JSONResponse:
    body: dict[str, Any] = {"type": "about:blank", "title": title, "status": status}
    if detail:
        body["detail"] = detail
    if code:
        body["code"] = code
    body["instance"] = request.url.path
    body.update(extra)
    return JSONResponse(body, status_code=status, media_type=PROBLEM_JSON, headers=headers)


async def _on_problem(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ProblemError)
    return _problem(request, exc.status, exc.title, exc.detail, exc.code, exc.headers, **exc.extra)


async def _on_http(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    try:
        title = HTTPStatus(exc.status_code).phrase
    except ValueError:
        title = "Error"
    detail = exc.detail if exc.detail != title else None
    return _problem(request, exc.status_code, title, detail, headers=dict(exc.headers or {}))


async def _on_validation(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    errors = [
        {"loc": [str(p) for p in e["loc"]], "msg": e["msg"], "type": e["type"]}
        for e in exc.errors()
    ]
    return _problem(
        request, 422, "Unprocessable Entity", "Request validation failed.", errors=errors
    )


async def _on_data_error(request: Request, exc: Exception) -> JSONResponse:
    # Input valid for pydantic but not storable, e.g. a NUL byte in text or JSON.
    return _problem(request, 422, "Unprocessable Entity", "Request contains invalid data.")


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ProblemError, _on_problem)
    app.add_exception_handler(DataError, _on_data_error)
    app.add_exception_handler(StarletteHTTPException, _on_http)
    app.add_exception_handler(RequestValidationError, _on_validation)
