"""REST API for the React dashboard. Authenticated with a signed session cookie.

Who may do what:
  admin  - manage servers and which of their tools are admin-only, invite users, change roles, issue and revoke
           anyone's keys, see the whole audit log; use every tool
  member - see servers and the tools they may use, manage their own keys, use the chat, see their own audit rows
Upstream credentials are write-only: the API accepts them but only ever answers with `has_credential`.
"""

from __future__ import annotations

import re
import secrets
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import delete, func, select

from . import chat, health
from .config import settings
from .db import AdminOnlyTool, ApiKey, AuditLog, Invite, Server, Session, User, aware, utcnow
from .router import admin_only_tools
from .security import hash_password, hash_token, new_api_key, verify_password

api = APIRouter(prefix="/api")
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


# ---------------- who is calling ----------------

async def current_user(request: Request) -> User:
    # CSRF protection: the dashboard sends this header on every write. A page on another site can't add a custom
    # header to a cross-site request without a CORS preflight, and we never allow one.
    if request.method not in SAFE_METHODS and request.headers.get("x-requested-with") != "dashboard":
        raise HTTPException(403, "Missing X-Requested-With header")
    uid = request.session.get("uid")
    if not uid:
        raise HTTPException(401, "Sign in first")
    async with Session() as db:
        user = await db.get(User, uid)
    if user is None or user.disabled:
        request.session.clear()
        raise HTTPException(401, "Your account is disabled")
    return user


async def admin_user(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403, "Admins only")
    return user


def user_out(u: User) -> dict:
    return {"id": u.id, "email": u.email, "name": u.name, "role": u.role, "disabled": u.disabled, "created_at": u.created_at}


# ---------------- sign in ----------------

class LoginIn(BaseModel):
    email: EmailStr
    password: str


@api.post("/auth/login")
async def login(body: LoginIn, request: Request):
    async with Session() as db:
        user = await db.scalar(select(User).where(func.lower(User.email) == body.email.lower()))
    # One message for every failure, so the answer doesn't reveal which emails have accounts.
    if user is None or user.disabled or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "Email or password is incorrect")
    request.session.clear()  # a fresh session on every sign-in (no session fixation)
    request.session["uid"] = user.id
    return user_out(user)


@api.post("/auth/logout")
async def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@api.get("/me")
async def me(user: User = Depends(current_user)):
    cfg = settings()
    return {"user": user_out(user), "mcp_url": cfg.public_url.rstrip("/") + "/mcp",
            "chat_configured": bool(cfg.llm_base_url and cfg.llm_api_key and cfg.llm_model)}


# ---------------- invites and users ----------------

class InviteIn(BaseModel):
    email: EmailStr
    role: Literal["admin", "member"] = "member"


@api.post("/invites")
async def create_invite(body: InviteIn, admin: User = Depends(admin_user)):
    async with Session() as db:
        if await db.scalar(select(User).where(func.lower(User.email) == body.email.lower())):
            raise HTTPException(409, "A user with this email already exists")
        token = secrets.token_urlsafe(24)
        inv = Invite(email=body.email, role=body.role, token_hash=hash_token(token), created_by=admin.id,
                     expires_at=utcnow() + timedelta(days=7))
        db.add(inv)
        await db.commit()
    # No email sending in this version: the admin copies the link to the teammate.
    return {"id": inv.id, "email": inv.email, "role": inv.role,
            "invite_url": f"{settings().public_url.rstrip('/')}/#/join/{token}"}


@api.get("/invites")
async def list_invites(_: User = Depends(admin_user)):
    async with Session() as db:
        rows = (await db.scalars(select(Invite).where(Invite.accepted_at.is_(None)).order_by(Invite.id.desc()))).all()
    return [{"id": i.id, "email": i.email, "role": i.role, "expires_at": i.expires_at} for i in rows]


@api.delete("/invites/{invite_id}")
async def delete_invite(invite_id: int, _: User = Depends(admin_user)):
    async with Session() as db:
        if inv := await db.get(Invite, invite_id):
            await db.delete(inv)
            await db.commit()
    return {"ok": True}


async def _valid_invite(db, token: str) -> Invite:
    inv = await db.scalar(select(Invite).where(Invite.token_hash == hash_token(token)))
    if inv is None or inv.accepted_at is not None or aware(inv.expires_at) < utcnow():
        raise HTTPException(404, "This invite link is invalid or has expired. Ask an admin for a new one.")
    return inv


@api.get("/join/{token}")
async def get_invite(token: str):
    async with Session() as db:
        inv = await _valid_invite(db, token)
    return {"email": inv.email, "role": inv.role}


class AcceptIn(BaseModel):
    token: str
    name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=10, max_length=200)


@api.post("/join")
async def accept_invite(body: AcceptIn, request: Request):
    async with Session() as db:
        inv = await _valid_invite(db, body.token)
        user = User(email=inv.email, name=body.name, role=inv.role, password_hash=hash_password(body.password))
        db.add(user)
        inv.accepted_at = utcnow()  # an invite link works once
        await db.commit()
    request.session.clear()
    request.session["uid"] = user.id
    return user_out(user)


@api.get("/users")
async def list_users(_: User = Depends(admin_user)):
    async with Session() as db:
        users = (await db.scalars(select(User).order_by(User.id))).all()
        active = dict((await db.execute(
            select(ApiKey.user_id, func.count()).where(ApiKey.revoked_at.is_(None)).group_by(ApiKey.user_id)
        )).all())
    return [{**user_out(u), "active_keys": active.get(u.id, 0)} for u in users]


class UserPatch(BaseModel):
    role: Literal["admin", "member"] | None = None
    disabled: bool | None = None


@api.patch("/users/{user_id}")
async def update_user(user_id: int, body: UserPatch, admin: User = Depends(admin_user)):
    if user_id == admin.id:
        raise HTTPException(400, "You cannot change your own role or disable yourself")  # never lock out the last admin
    async with Session() as db:
        u = await db.get(User, user_id)
        if u is None:
            raise HTTPException(404, "No such user")
        if body.role is not None:
            u.role = body.role
        if body.disabled is not None:
            u.disabled = body.disabled  # a disabled user's keys stop working too (see mcp_endpoint.py)
        await db.commit()
    return user_out(u)


# ---------------- API keys ----------------

class KeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    user_id: int | None = None  # admins may issue a key for someone else


def key_out(k: ApiKey, owner: User) -> dict:
    return {"id": k.id, "name": k.name, "prefix": k.prefix, "user_id": k.user_id, "owner_email": owner.email,
            "created_at": k.created_at, "last_used_at": k.last_used_at, "revoked_at": k.revoked_at}


@api.get("/keys")
async def list_keys(all: bool = False, user: User = Depends(current_user)):
    async with Session() as db:
        q = select(ApiKey, User).join(User, User.id == ApiKey.user_id).order_by(ApiKey.id.desc())
        if not (all and user.role == "admin"):
            q = q.where(ApiKey.user_id == user.id)
        rows = (await db.execute(q)).all()
    return [key_out(k, u) for k, u in rows]


@api.post("/keys")
async def create_key(body: KeyIn, user: User = Depends(current_user)):
    owner_id = body.user_id or user.id
    if owner_id != user.id and user.role != "admin":
        raise HTTPException(403, "Only admins can issue keys for other users")
    key, prefix, digest = new_api_key()
    async with Session() as db:
        owner = await db.get(User, owner_id)
        if owner is None or owner.disabled:
            raise HTTPException(404, "No such active user")
        k = ApiKey(user_id=owner_id, name=body.name, prefix=prefix, key_hash=digest, created_by=user.id)
        db.add(k)
        await db.commit()
    return {**key_out(k, owner), "key": key}  # the only time the key itself leaves the gateway


@api.delete("/keys/{key_id}")
async def revoke_key(key_id: int, user: User = Depends(current_user)):
    async with Session() as db:
        k = await db.get(ApiKey, key_id)
        if k is None or (k.user_id != user.id and user.role != "admin"):
            raise HTTPException(404, "No such key")  # 404 for someone else's key: don't confirm it exists
        if k.revoked_at is None:
            k.revoked_at = utcnow()  # kept, not deleted, so the audit log can still name the key's owner
            await db.commit()
    return {"ok": True}


# ---------------- upstream servers ----------------

# Lowercase letters, digits and single hyphens, no underscores: then "__" can only ever be the separator in
# exposed tool names (woo__list_orders), and splitting on it is unambiguous.
SLUG = re.compile(r"^[a-z][a-z0-9-]{1,22}$")


class ServerIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    slug: str
    url: str = Field(max_length=500, pattern=r"^https?://")
    auth_type: Literal["none", "bearer", "header"] = "none"
    auth_header: str | None = Field(default=None, max_length=100)  # the header's name when auth_type is "header"
    credential: str | None = Field(default=None, max_length=4000)
    enabled: bool = True
    admin_only_tools: list[str] = Field(default=[], max_length=500)  # the upstream's tool names, e.g. create_product

    @field_validator("slug")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not SLUG.match(v):
            raise ValueError("2-23 characters: lowercase letters, digits and hyphens, starting with a letter")
        return v


class ServerPatch(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    url: str | None = Field(default=None, max_length=500, pattern=r"^https?://")
    auth_type: Literal["none", "bearer", "header"] | None = None
    auth_header: str | None = Field(default=None, max_length=100)
    credential: str | None = Field(default=None, max_length=4000)  # left out = keep the stored one; "" = clear it
    enabled: bool | None = None
    admin_only_tools: list[str] | None = Field(default=None, max_length=500)  # left out = keep; a list replaces them


def server_out(s: Server, user: User, locked: set[tuple[int, str]]) -> dict:
    """`locked`: the admin-only tools (admin_only_tools()). Members don't see those tools at all."""
    tools = [{"name": f"{s.slug}__{t['name']}", "description": (t.get("description") or "")[:300],
              "admin_only": (s.id, t["name"]) in locked} for t in s.tools or []]
    out = {
        "id": s.id, "slug": s.slug, "name": s.name, "enabled": s.enabled,
        "status": "disabled" if not s.enabled else s.status,
        "last_checked_at": s.last_checked_at,
        "tools": tools if user.role == "admin" else [t for t in tools if not t["admin_only"]],
    }
    if user.role == "admin":
        # Admins also see where it points and how it authenticates, but never the credential itself.
        out |= {"url": s.url, "auth_type": s.auth_type, "auth_header": s.auth_header,
                "has_credential": bool(s.credential), "last_error": s.last_error}
    return out


async def set_admin_only(db, server_id: int, tools: list[str]) -> None:
    """Replace a server's admin-only tools with `tools` (the upstream's own names)."""
    await db.execute(delete(AdminOnlyTool).where(AdminOnlyTool.server_id == server_id))
    for tool in sorted({t.strip() for t in tools if t.strip()}):
        db.add(AdminOnlyTool(server_id=server_id, tool=tool[:200]))


@api.get("/servers")
async def list_servers(user: User = Depends(current_user)):
    async with Session() as db:
        rows = (await db.scalars(select(Server).order_by(Server.id))).all()
        locked = await admin_only_tools(db)
    return [server_out(s, user, locked) for s in rows]


@api.post("/servers")
async def create_server(body: ServerIn, admin: User = Depends(admin_user)):
    async with Session() as db:
        if await db.scalar(select(Server).where(Server.slug == body.slug)):
            raise HTTPException(409, f"The prefix '{body.slug}' is taken")
        s = Server(name=body.name, slug=body.slug, url=body.url, auth_type=body.auth_type, auth_header=body.auth_header,
                   credential=body.credential if body.credential and body.auth_type != "none" else None,
                   enabled=body.enabled)
        db.add(s)
        await db.flush()  # assigns s.id
        await set_admin_only(db, s.id, body.admin_only_tools)
        await db.commit()
    return await _checked(s.id, admin)


async def _checked(server_id: int, admin: User) -> dict:
    """Check the server right away, so the admin sees Connected or the reason it isn't, not a 10 s wait."""
    await health.refresh_server(server_id)
    async with Session() as db:
        s = await db.get(Server, server_id)
        if s is None:
            raise HTTPException(404, "No such server")
        return server_out(s, admin, await admin_only_tools(db, server_id))


@api.post("/servers/{server_id}/refresh")
async def refresh_server(server_id: int, admin: User = Depends(admin_user)):
    return await _checked(server_id, admin)


@api.patch("/servers/{server_id}")
async def update_server(server_id: int, body: ServerPatch, admin: User = Depends(admin_user)):
    async with Session() as db:
        s = await db.get(Server, server_id)
        if s is None:
            raise HTTPException(404, "No such server")
        for field in ("name", "url", "auth_type", "auth_header", "enabled"):
            if (value := getattr(body, field)) is not None:
                setattr(s, field, value)
        if body.credential is not None:
            s.credential = body.credential or None
        if s.auth_type == "none":
            s.credential = None  # "no authentication" means nothing stored, not an old credential left behind
        if body.enabled is False:
            s.status = "unknown"
        if body.admin_only_tools is not None:
            await set_admin_only(db, s.id, body.admin_only_tools)
        await db.commit()
    return await _checked(server_id, admin)


@api.delete("/servers/{server_id}")
async def delete_server(server_id: int, _: User = Depends(admin_user)):
    async with Session() as db:
        if s := await db.get(Server, server_id):
            # Explicitly, not only by the foreign key's ON DELETE CASCADE: SQLite (the tests) doesn't enforce it.
            await db.execute(delete(AdminOnlyTool).where(AdminOnlyTool.server_id == server_id))
            await db.delete(s)
            await db.commit()
    return {"ok": True}


# ---------------- audit log ----------------

@api.get("/audit")
async def audit_log(
    limit: int = 200, user_id: int | None = None, server: str | None = None, outcome: str | None = None,
    via: str | None = None, user: User = Depends(current_user),
):
    """Newest first. Admins see everyone's calls and can filter by user; members only ever see their own."""
    q = select(AuditLog).order_by(AuditLog.id.desc()).limit(min(max(limit, 1), 1000))
    if user.role != "admin":
        q = q.where(AuditLog.user_id == user.id)
    elif user_id:
        q = q.where(AuditLog.user_id == user_id)
    if server:
        q = q.where(AuditLog.server_slug == server)
    if outcome:
        q = q.where(AuditLog.outcome == outcome)
    if via:
        q = q.where(AuditLog.via == via)
    async with Session() as db:
        rows = (await db.scalars(q)).all()
    return [{"id": r.id, "ts": r.ts, "user_email": r.user_email, "via": r.via, "server": r.server_slug, "tool": r.tool,
             "duration_ms": r.duration_ms, "outcome": r.outcome, "detail": r.detail} for r in rows]


# ---------------- chat ----------------

class ChatIn(BaseModel):
    messages: list[dict[str, str]] = Field(min_length=1, max_length=40)


@api.post("/chat")
async def chat_endpoint(body: ChatIn, user: User = Depends(current_user)):
    # The agent acts as the signed-in user (from the session cookie): its tool calls are audited under them.
    try:
        return await chat.run(user, body.messages)
    except chat.ChatUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
