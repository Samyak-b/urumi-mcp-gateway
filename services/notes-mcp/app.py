"""Team notes MCP server: a second upstream for the gateway, one we control and can run inside the cluster.

It is there to show three gateway behaviours:
  * a static bearer credential that the gateway injects (members never see it),
  * a prompt that needs two servers ("summarise today's orders and save it as a note"),
  * failure isolation: kill this pod and the store's tools keep working.

Notes live in memory on purpose: restarting the pod clears them. That keeps it a single small process with no
storage to manage; a real notes service would use a database.
"""

from __future__ import annotations

import hmac
import itertools
import logging
import os
from datetime import datetime, timezone
from typing import Annotated, Any

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("notes-mcp")

TOKEN = os.environ.get("MCP_TOKEN", "")
_ids = itertools.count(1)
NOTES: dict[int, dict[str, Any]] = {}

mcp = FastMCP(
    "team-notes",
    instructions="A shared notepad for the team. Save summaries and look them up later.",
    stateless_http=True,
    json_response=True,
    # Called by Service name from the gateway, not by a browser on localhost: see woo-mcp/app.py.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
async def add_note(
    title: Annotated[str, Field(min_length=1, max_length=200)],
    body: Annotated[str, Field(max_length=20000)],
    tags: Annotated[list[str], Field(max_length=10)] = [],
) -> dict[str, Any]:
    """Save a note for the team. Returns the stored note with its id."""
    note = {"id": next(_ids), "title": title, "body": body, "tags": tags, "created": datetime.now(timezone.utc).isoformat()}
    NOTES[note["id"]] = note
    return note


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_notes(tag: Annotated[str | None, Field(description="Only notes with this tag")] = None) -> dict[str, Any]:
    """List saved notes, newest first, optionally filtered by tag."""
    notes = [n for n in NOTES.values() if tag is None or tag in n["tags"]]
    return {"notes": sorted(notes, key=lambda n: n["id"], reverse=True)}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def search(query: Annotated[str, Field(min_length=1)]) -> dict[str, Any]:
    """Find notes whose title or body contains the query (case-insensitive)."""
    q = query.lower()
    return {"notes": [n for n in NOTES.values() if q in n["title"].lower() or q in n["body"].lower()]}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
async def delete_note(note_id: int) -> dict[str, Any]:
    """Delete a note by id."""
    if NOTES.pop(note_id, None) is None:
        raise ValueError(f"No note with id {note_id}. Use list_notes to see the ids.")
    return {"deleted": note_id}


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


class BearerAuth:
    """ASGI middleware: /mcp requests must carry the token the gateway presents (see woo-mcp/app.py)."""

    def __init__(self, app, token: str):
        self.app, self.token = app, token.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/mcp") and self.token:
            auth = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(auth, b"Bearer " + self.token):
                return await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
        return await self.app(scope, receive, send)


if not TOKEN:
    log.warning("MCP_TOKEN is empty: /mcp is unauthenticated (acceptable only in tests)")
app = BearerAuth(mcp.streamable_http_app(), TOKEN)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
