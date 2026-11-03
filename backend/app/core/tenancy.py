"""Structural tenant isolation (ADR-0003). Fail-closed.

Tenant models (`TenantMixin`) can only be touched inside an org context or an explicit
`system_scope()`. The guard has three layers:

- `do_orm_execute` walks the *whole* statement (FROMs, joins, subqueries, EXISTS, IN). If any
  tenant table appears: no context raises; with a context the org filter is applied to every ORM
  entity; a tenant table reached through plain Core (no ORM entity, so it cannot be filtered)
  raises. Raw `text()` SQL is invisible to the guard and must not touch tenant tables.
- `before_flush` rejects inserts, updates and deletes of tenant objects of another org, and any
  change of `organization_id`.
- `Session.get`/`merge` results are verified, because an identity-map hit issues no SQL.

Postgres RLS is not enabled in Phase 1, but `app.org_id` is already set per transaction so it can
be later.

Lookups that must run *before* an org context exists (resolving a membership from the URL org and
the JWT subject) cannot go through a tenant model: use `system_scope()` for them.
"""

import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from itertools import chain
from typing import Any, Literal, cast

from sqlalchemy import ForeignKey, Select, Table, event, inspect, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import (
    Mapped,
    ORMExecuteState,
    Session,
    SessionTransaction,
    mapped_column,
    with_loader_criteria,
)
from sqlalchemy.sql.elements import ColumnClause
from sqlalchemy.sql.selectable import Join
from sqlalchemy.sql.visitors import iterate

from app.core.ids import new_id


@dataclass(frozen=True)
class OrgContext:
    """Who is acting inside which org. Built once per request from the URL org plus a live
    membership lookup (never from the token or body).

    Rules for whoever builds it:
    - API-key callers: the URL org must equal the key's `organization_id`, otherwise 404.
    - `permissions` is the role's permissions intersected with the key's scopes.
    - A key acts only through a membership of its service principal; no membership, no context.
    - `environment_id` is the key's environment (None = unrestricted) and restricts env-scoped
      actions; `actor_kind` says which credential type the caller used.
    """

    org_id: uuid.UUID
    principal_id: uuid.UUID
    role: str
    permissions: frozenset[str]
    environment_id: uuid.UUID | None = None
    actor_kind: Literal["user", "api_key"] = "user"


class TenantContextError(RuntimeError):
    """A tenant model was used with no org context, the wrong org, or in an unfilterable way."""


class TenantMixin:
    """Primary key plus a NOT NULL organization_id. Subclasses add UNIQUE(organization_id, id)."""

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"))


_org_ctx: ContextVar[OrgContext | None] = ContextVar("org_ctx", default=None)
_system: ContextVar[bool] = ContextVar("system_scope", default=False)
_SET_ORG = text("SELECT set_config('app.org_id', :org, true)")


def current_org() -> OrgContext | None:
    return _org_ctx.get()


@contextmanager
def org_scope(ctx: OrgContext) -> Iterator[OrgContext]:
    token = _org_ctx.set(ctx)
    try:
        yield ctx
    finally:
        _org_ctx.reset(token)


@contextmanager
def system_scope() -> Iterator[None]:
    """The only escape from the tenant guard (job claimer, reaper, pre-context lookups)."""
    token = _system.set(True)
    try:
        yield
    finally:
        _system.reset(token)


@asynccontextmanager
async def bind_org(session: AsyncSession, ctx: OrgContext) -> AsyncIterator[OrgContext]:
    """Hold `ctx` for the duration of the block, then reset it.

    Use it inside an *async* yield-dependency so the context lives exactly as long as the request:

        async def org_dep(...) -> AsyncIterator[OrgContext]:
            async with bind_org(session, ctx):
                yield ctx

    Async dependencies and the endpoint share one task, so the ContextVar is visible to the
    endpoint; a sync (threadpool) dependency would not propagate it. It also sets `app.org_id` on
    a transaction the membership lookup already opened; later ones get it from `after_begin`.
    """
    token = _org_ctx.set(ctx)
    try:
        if session.in_transaction():
            await session.execute(_SET_ORG, {"org": str(ctx.org_id)})
        yield ctx
    finally:
        _org_ctx.reset(token)


async def get_in_org[T: TenantMixin](
    session: AsyncSession, model: type[T], id: uuid.UUID
) -> T | None:
    """Org-scoped get-by-id for tenant models (the guard also verifies `session.get`)."""
    obj = (await session.execute(select(model).where(model.id == id))).scalar_one_or_none()
    return _verified(obj)


def _tenant_table_names() -> frozenset[str]:
    names: set[str] = set()
    stack = list(TenantMixin.__subclasses__())
    while stack:
        cls = stack.pop()
        stack.extend(cls.__subclasses__())
        table = getattr(cls, "__table__", None)
        if isinstance(table, Table):
            names.add(table.name)
    return frozenset(names)


def _verified[T](obj: T) -> T:
    if isinstance(obj, TenantMixin) and not _system.get():
        ctx = _org_ctx.get()
        if ctx is None or obj.organization_id != ctx.org_id:
            raise TenantContextError("tenant object does not belong to the current org context")
    return obj


def _bare_tenant_from(statement: Any, names: frozenset[str]) -> bool:
    """True if a tenant table is reached as plain Core, which loader criteria can't filter.

    Every ORM entity use (also inside subqueries, EXISTS and IN) is annotated with "parententity";
    a tenant Table or one of its columns without it is Core SQL.
    """

    def core(el: Any) -> bool:
        if "parententity" in el._annotations:
            return False
        if isinstance(el, Table):
            return el.name in names
        table = el.table if isinstance(el, ColumnClause) else None
        return isinstance(table, Table) and table.name in names

    def core_from(f: Any) -> bool:
        return core_from(f.left) or core_from(f.right) if isinstance(f, Join) else core(f)

    for el in iterate(statement):
        if isinstance(el, Select):
            if any(core(r) for r in el._raw_columns) or any(core_from(f) for f in el._from_obj):
                return True
        elif isinstance(el, ColumnClause) and core(el):
            return True
    return False


@event.listens_for(Session, "do_orm_execute")
def _guard_tenant_queries(state: ORMExecuteState) -> None:
    names = _tenant_table_names()
    statement = cast(Any, state.statement)
    if not any(isinstance(e, Table) and e.name in names for e in iterate(statement)):
        return
    if _system.get():
        return
    ctx = _org_ctx.get()
    if ctx is None:
        raise TenantContextError("tenant table used without an org context")
    if state.is_insert:
        # A flush never reaches this hook, so a statement insert would skip the before_flush guard.
        raise TenantContextError("statement insert into a tenant table; use session.add")
    if not state.is_orm_statement or _bare_tenant_from(statement, names):
        raise TenantContextError("tenant table used through Core SQL; use ORM entities")
    org_id = ctx.org_id
    state.statement = state.statement.options(
        with_loader_criteria(
            TenantMixin,
            lambda cls: cls.organization_id == org_id,
            include_aliases=True,
        )
    )


def _org_id_changed(obj: TenantMixin) -> bool:
    state = inspect(obj)
    return state is not None and state.attrs.organization_id.history.has_changes()


@event.listens_for(Session, "before_flush")
def _guard_flush(session: Session, flush_context: Any, instances: Any) -> None:
    for obj in session.dirty:
        if isinstance(obj, TenantMixin) and _org_id_changed(obj):
            raise TenantContextError("organization_id of a tenant row cannot change")
    if _system.get():
        return
    ctx = _org_ctx.get()
    for obj in chain(session.new, session.dirty, session.deleted):
        if isinstance(obj, TenantMixin) and (ctx is None or obj.organization_id != ctx.org_id):
            raise TenantContextError("write to a tenant row outside the current org context")


@event.listens_for(Session, "after_begin")
def _apply_org_to_transaction(
    session: Session, transaction: SessionTransaction, connection: Connection
) -> None:
    ctx = _org_ctx.get()
    if ctx is not None:
        connection.execute(_SET_ORG, {"org": str(ctx.org_id)})


def _wrap(original: Any) -> Any:
    def wrapper(self: Session, *args: Any, **kwargs: Any) -> Any:
        return _verified(original(self, *args, **kwargs))

    wrapper.__name__ = original.__name__
    wrapper.__doc__ = original.__doc__
    return wrapper


# An identity-map hit in get() issues no SQL, so verify what comes back.
Session.get = _wrap(Session.get)  # type: ignore[method-assign]
Session.merge = _wrap(Session.merge)  # type: ignore[method-assign]
