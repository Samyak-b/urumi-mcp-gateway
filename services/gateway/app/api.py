"""REST API for the React dashboard. Authenticated with a signed session cookie.

Who may do what:
  admin  - manage servers, invite users, change roles, issue and revoke anyone's keys, see the whole audit log
  member - see servers and their tools, manage their own keys, use the chat, see their own audit rows
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select

from .config import settings
from .db import ApiKey, Invite, Session, User, aware, utcnow
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
    return {"user": user_out(user), "mcp_url": settings().public_url.rstrip("/") + "/mcp"}


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
