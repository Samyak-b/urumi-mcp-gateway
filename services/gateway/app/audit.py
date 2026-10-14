"""The audit log: one row per tool call, whoever made it and however it ended.

Every outcome passes through record(): calls from MCP clients and from the chat agent (router.call_tool), and
calls refused at the door because the key was revoked or its user disabled (mcp_endpoint.py).
"""

from __future__ import annotations

from .db import AuditLog, Session, User


async def record(user: User | None, via: str, slug: str | None, tool: str | None, ms: int, outcome: str,
                 detail: str | None = None) -> None:
    async with Session() as db:
        db.add(AuditLog(
            user_id=user.id if user else None,
            user_email=user.email if user else None,
            via=via, server_slug=slug, tool=tool, duration_ms=ms, outcome=outcome,
            detail=(detail or "")[:1000] or None,
        ))
        await db.commit()
