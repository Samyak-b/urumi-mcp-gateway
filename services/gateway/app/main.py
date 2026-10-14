"""Gateway process: the dashboard REST API (/api), the MCP endpoint (/mcp) and health probes."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from .config import settings
from .db import Server, Session, User, init_db, ping_db, wait_for_db
from .security import hash_password

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("gateway")


async def bootstrap() -> None:
    """First start: create the admin account and register the seed servers (WooCommerce first)."""
    cfg = settings()
    async with Session() as db:
        if not await db.scalar(select(func.count()).select_from(User)):
            if not cfg.admin_password:
                raise RuntimeError("No users exist and ADMIN_PASSWORD is not set")
            db.add(User(email=cfg.admin_email, name="Admin", role="admin", password_hash=hash_password(cfg.admin_password)))
            log.info("created the first admin %s", cfg.admin_email)
        for seed in json.loads(cfg.seed_servers or "[]"):
            if await db.scalar(select(Server).where(Server.slug == seed["slug"])):
                continue  # seeded once; after that the dashboard owns the record
            db.add(
                Server(
                    slug=seed["slug"], name=seed["name"], url=seed["url"],
                    auth_type=seed.get("auth_type", "none"), auth_header=seed.get("auth_header"),
                    credential=seed.get("credential") or None,
                )
            )
            log.info("registered seed server %s -> %s", seed["slug"], seed["url"])
        await db.commit()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await wait_for_db()
    await init_db()
    await bootstrap()
    yield


if not settings().session_secret:
    # It signs the session cookies; without it (or with a guessable one) anyone could forge a login.
    raise RuntimeError("SESSION_SECRET is not set")

app = FastAPI(title="Team MCP Gateway", lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")


@app.get("/healthz")
async def healthz():
    """Liveness: the process answers. Deliberately no database check, so a DB blip doesn't restart the pod."""
    return {"ok": True}


@app.get("/readyz")
async def readyz():
    """Readiness: the process answers and reaches its database; otherwise Kubernetes stops sending traffic."""
    try:
        await asyncio.wait_for(ping_db(), timeout=3)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "db": str(exc)[:200]}, status_code=503)
    return {"ok": True}
