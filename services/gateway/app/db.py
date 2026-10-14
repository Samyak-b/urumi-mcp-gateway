"""Database models and connection. Postgres in the cluster; SQLite in local tests.

Tables are created at startup with create_all. That is enough for a first version; a real product would use
migrations (Alembic) so that a schema change does not need a fresh database.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .config import settings

log = logging.getLogger("gateway.db")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: datetime) -> datetime:
    """SQLite drops the timezone, Postgres keeps it; always compare in UTC."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    role: Mapped[str] = mapped_column(String(16), default="member")  # "admin" | "member"
    password_hash: Mapped[str] = mapped_column(String(255))
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ApiKey(Base):
    """A user's key for the MCP endpoint. Only its SHA-256 hash is stored; the key itself is shown once."""

    __tablename__ = "api_keys"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    prefix: Mapped[str] = mapped_column(String(16))  # the first characters, so a user can recognise the key
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)  # differs from user_id if an admin issued it
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Invite(Base):
    """An invitation link. Like API keys, only the token's hash is stored."""

    __tablename__ = "invites"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_by: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Server(Base):
    """An upstream MCP server. `slug` is the prefix of its tools' names (woo -> woo__list_orders)."""

    __tablename__ = "servers"
    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100))
    url: Mapped[str] = mapped_column(String(500))
    auth_type: Mapped[str] = mapped_column(String(16), default="none")  # none | bearer | header
    auth_header: Mapped[str | None] = mapped_column(String(100), nullable=True)  # header name when auth_type=header
    credential: Mapped[str | None] = mapped_column(Text, nullable=True)  # never returned by the API
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Health and tool cache, kept current by the background health check.
    status: Mapped[str] = mapped_column(String(16), default="unknown")  # connected | unreachable | unknown
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    tools: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AuditLog(Base):
    """One row per tool call, from MCP clients and from the chat agent."""

    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    user_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    user_email: Mapped[str | None] = mapped_column(String(255), nullable=True)  # kept even if the user is deleted
    via: Mapped[str] = mapped_column(String(8))  # "mcp" (external client) | "chat" (dashboard agent)
    server_slug: Mapped[str | None] = mapped_column(String(24), nullable=True)
    tool: Mapped[str | None] = mapped_column(String(200), nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    # ok | error (the upstream answered with an error) | timeout | unreachable | denied | rejected (revoked key)
    outcome: Mapped[str] = mapped_column(String(16))
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)


engine = create_async_engine(settings().database_url, pool_pre_ping=True)
Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def ping_db() -> None:
    async with engine.connect() as conn:
        await conn.execute(text("select 1"))


async def wait_for_db(wait_s: float = 45) -> None:
    """On a fresh install Postgres often starts after the gateway. Wait for it instead of crash-looping into
    Kubernetes' growing restart back-off. 45 s stays inside the liveness probe's window, so a real outage still
    restarts the pod."""
    deadline = asyncio.get_running_loop().time() + wait_s
    while True:
        try:
            await ping_db()
            return
        except (OSError, SQLAlchemyError) as exc:
            if asyncio.get_running_loop().time() > deadline:
                raise
            log.warning("database not reachable yet (%s); retrying", exc.__class__.__name__)
            await asyncio.sleep(2)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
