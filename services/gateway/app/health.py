"""Background health check: keeps each server's status and tool list current in the database.

Tool listing (router.py) reads this cached list, never the upstream itself, so a slow or dead server can't make
anyone's tools/list wait or fail. Status is Connected / Unreachable / Disabled (+ the reason) on the dashboard.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from . import upstream
from .config import settings
from .db import Server, Session, utcnow

log = logging.getLogger("gateway.health")


async def mark(server_id: int, status: str, error: str | None, tools: list | None = None) -> None:
    async with Session() as db:
        s = await db.get(Server, server_id)
        if s is None:
            return
        s.status, s.last_error, s.last_checked_at = status, error, utcnow()
        if tools is not None:
            s.tools = tools
        await db.commit()


async def refresh_server(server_id: int) -> None:
    async with Session() as db:
        s = await db.get(Server, server_id)
    if s is None or not s.enabled:
        return  # a disabled server is not checked; the API reports it as "disabled"
    try:
        tools = await upstream.probe(upstream.UpstreamTarget.from_server(s))
        await mark(s.id, "connected", None, tools)
    except upstream.UpstreamError as exc:
        # Keep the last known tool list so the dashboard can still show it; status != connected already removes
        # it from tools/list.
        await mark(s.id, "unreachable", str(exc))
    except Exception as exc:  # noqa: BLE001 - a bug in one check must not stop the others
        log.exception("health check of %s failed", s.slug)
        await mark(s.id, "unreachable", f"internal error: {exc.__class__.__name__}")


async def refresh_all() -> None:
    async with Session() as db:
        ids = (await db.scalars(select(Server.id).where(Server.enabled.is_(True)))).all()
    # Concurrent, each with its own deadline: one slow server can't delay the others' status.
    await asyncio.gather(*(refresh_server(i) for i in ids))


async def loop() -> None:
    while True:
        try:
            await refresh_all()
        except Exception:  # noqa: BLE001
            log.exception("health check round failed")
        await asyncio.sleep(settings().health_interval_s)
