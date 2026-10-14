"""The gateway core: where tools are combined, routed and audited.

Both front doors use exactly these functions:
  * the MCP endpoint (/mcp), used by external clients such as Claude Code, and
  * the dashboard's chat agent.
So which servers are enabled, how credentials are injected and what gets audited can't differ between them.

Tool naming scheme (how name collisions are handled):
  exposed name = "<server slug>__<upstream tool name>",  e.g. woo__list_orders, notes__search
Slugs are unique and can't contain "_" (api.py), so the first "__" always splits a name unambiguously, and two
servers that both have a "search" tool become notes__search and wiki2__search. Names are cut down to the
characters and the 64-character length MCP clients accept (a long name gets a short hash so it stays unique).
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from mcp.types import CallToolResult, TextContent, Tool
from sqlalchemy import select

from . import audit, health, upstream
from .db import Server, Session, User

log = logging.getLogger("gateway.router")

SEP = "__"
_BAD = re.compile(r"[^A-Za-z0-9_-]")
MAX_NAME = 64
# Upstream descriptions are text from a server we don't control, and they go straight to the model.
# Bounding them limits how much an untrusted server can say to it (see docs/THREAT_MODEL.md).
MAX_DESCRIPTION = 1024


def exposed_name(slug: str, tool: str) -> str:
    name = f"{slug}{SEP}{_BAD.sub('_', tool)}"
    if len(name) > MAX_NAME:
        name = name[: MAX_NAME - 7] + "_" + hashlib.sha256(tool.encode()).hexdigest()[:6]
    return name


@dataclass
class RoutedTool:
    exposed: Tool  # what the client sees
    slug: str
    upstream_name: str  # the tool's real name on its server


def _tools_of(server: Server) -> list[RoutedTool]:
    out = []
    for t in server.tools or []:
        desc = (t.get("description") or "").strip()
        if len(desc) > MAX_DESCRIPTION:
            desc = desc[:MAX_DESCRIPTION] + "…"
        tool = Tool(
            name=exposed_name(server.slug, t["name"]),
            title=t.get("title"),
            description=f"[{server.name}] {desc}",  # which server it comes from, in front of the server's own text
            inputSchema=t.get("inputSchema") or {"type": "object", "properties": {}},
            annotations=t.get("annotations"),
        )
        out.append(RoutedTool(tool, server.slug, t["name"]))
    return out


async def routable_tools(user: User) -> list[RoutedTool]:
    """The tools of every enabled, currently connected server.

    Reads the tool lists the health check cached in the database. Listing therefore never waits on an upstream,
    so a slow or dead server can't break tools/list for anyone. `user` is where per-user rules plug in.
    """
    async with Session() as db:
        servers = (await db.scalars(
            select(Server).where(Server.enabled.is_(True), Server.status == "connected").order_by(Server.id)
        )).all()
    return [rt for s in servers for rt in _tools_of(s)]


def error_result(text: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], isError=True)


async def call_tool(user: User, name: str, arguments: dict[str, Any], via: str) -> CallToolResult:
    """Route one tool call to its server and audit it. Never raises: every failure comes back as an isError
    result, and every path writes exactly one audit row."""
    started = time.monotonic()
    ms = lambda: int((time.monotonic() - started) * 1000)  # noqa: E731
    slug, _, _ = name.partition(SEP)
    async with Session() as db:
        server = await db.scalar(select(Server).where(Server.slug == slug))

    # Checked again here, not only when listing: a client may call a tool from an old list after the server was
    # disabled, or make up a name.
    if server is None or not server.enabled:
        # An unknown prefix comes from the caller, so it isn't recorded as a server; the full name is in `tool`.
        await audit.record(user, via, server.slug if server else None, name, ms(), "denied", "unknown or disabled server")
        return error_result(f"Tool '{name}' is not available: server '{slug}' is unknown or disabled.")
    match = next((t for t in server.tools or [] if exposed_name(server.slug, t["name"]) == name), None)
    if match is None:
        await audit.record(user, via, slug, name, ms(), "denied", "unknown tool")
        return error_result(f"Server '{slug}' has no tool called '{name}'. List the tools again.")

    try:
        # The server's credential is added here, on the gateway. The caller's own API key is never forwarded.
        result = await upstream.call(upstream.UpstreamTarget.from_server(server), match["name"], arguments)
    except upstream.UpstreamError as exc:
        await audit.record(user, via, slug, match["name"], ms(), exc.kind, str(exc))
        if exc.kind == "unreachable":
            # Passive health check: hide a server we can't connect to now, not at the next check.
            # An error *answer* means the server is alive, so its tools stay listed.
            await health.mark(server.id, "unreachable", str(exc))
        return error_result(f"Upstream error ({exc.kind}): {exc}")
    except Exception as exc:  # noqa: BLE001 - a gateway bug must still be audited and answered
        log.exception("tool call %s failed inside the gateway", name)
        await audit.record(user, via, slug, match["name"], ms(), "error", f"internal gateway error ({exc.__class__.__name__})")
        return error_result(f"The gateway hit an internal error ({exc.__class__.__name__}) while calling this tool.")

    detail = None
    if result.isError:  # the tool itself reported a failure (e.g. "Not found"): keep its message
        detail = " ".join(c.text for c in result.content if isinstance(c, TextContent))[:500]
    await audit.record(user, via, slug, match["name"], ms(), "error" if result.isError else "ok", detail)
    return result
