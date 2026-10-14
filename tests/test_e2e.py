"""End-to-end test of the gateway without Kubernetes.

    make test        (runs this in the gateway image)
    python tests/test_e2e.py   (needs services/gateway/requirements.txt installed)

In one process it starts the real gateway (on SQLite), the real woo-mcp and notes-mcp servers, and three fakes:
a WooCommerce REST API, a slow MCP server whose tool name collides with notes' "search", and a scripted LLM.
Then it walks the definition of done and the gateway's failure handling: tool listing and routing, the collision
scheme, timeouts, a dead upstream, revoked keys, disabled servers, the chat agent acting as the user, credentials
never leaving the gateway, and the audit log. Exit code = number of failed checks.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import socket
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "tests"), str(ROOT / "services" / "gateway"), str(ROOT / "services" / "woo-mcp")]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


PORTS = {name: free_port() for name in ("gateway", "woo", "woo_api", "notes", "slow", "llm")}
URL = {name: f"http://127.0.0.1:{port}" for name, port in PORTS.items()}
WOO_TOKEN, NOTES_TOKEN = "woo-upstream-secret-1111", "notes-upstream-secret-2222"
ADMIN_PASSWORD = "admin-password-e2e"

# Configuration is read from the environment at import time, so set it before importing anything.
os.environ.update({
    "DATABASE_URL": f"sqlite+aiosqlite:///{tempfile.mkdtemp()}/gateway.db",
    "SESSION_SECRET": "test-session-secret", "ADMIN_EMAIL": "admin@example.com", "ADMIN_PASSWORD": ADMIN_PASSWORD,
    "PUBLIC_URL": URL["gateway"], "HEALTH_INTERVAL_S": "1", "UPSTREAM_CALL_TIMEOUT_S": "3",
    # The test's MCP servers listen on 127.0.0.1, which the SSRF guard refuses unless listed (as in-cluster services are).
    "INTERNAL_UPSTREAM_HOSTS": "127.0.0.1",
    "LLM_BASE_URL": URL["llm"] + "/v1", "LLM_MODEL": "scripted", "LLM_API_KEY": "llm-secret-3333",
    "SEED_SERVERS": json.dumps([
        {"slug": "woo", "name": "WooCommerce store", "url": URL["woo"] + "/mcp", "auth_type": "bearer", "credential": WOO_TOKEN,
         "admin_only_tools": ["create_product", "update_order_status"]},
        {"slug": "notes", "name": "Team notes", "url": URL["notes"] + "/mcp", "auth_type": "bearer", "credential": NOTES_TOKEN},
        {"slug": "slow", "name": "Slow server", "url": URL["slow"] + "/mcp"},
    ]),
    "WOO_BASE_URL": URL["woo_api"], "WOO_CONSUMER_KEY": "ck_test", "WOO_CONSUMER_SECRET": "cs_test",
    "STORE_PUBLIC_URL": "http://store.test",
})

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

import fakes  # noqa: E402


def load(name: str, path: Path, env: dict):
    """Load one of the MCP servers' app.py files under its own module name, with its own environment."""
    os.environ.update(env)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


woo_mcp = load("woo_mcp_app", ROOT / "services/woo-mcp/app.py", {"MCP_TOKEN": WOO_TOKEN})
notes_mcp = load("notes_mcp_app", ROOT / "services/notes-mcp/app.py", {"MCP_TOKEN": NOTES_TOKEN})
from app.main import app as gateway_app  # noqa: E402

failures = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failures
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""), flush=True)


TASKS: list[asyncio.Task] = []


async def serve(app, port: int) -> uvicorn.Server:
    # A short graceful-shutdown window: the slow server still has a 30 s call in flight when the test ends.
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on",
                                           timeout_graceful_shutdown=1))
    TASKS.append(asyncio.create_task(server.serve()))
    while not server.started:
        await asyncio.sleep(0.05)
    return server


class Dashboard:
    def __init__(self):
        self.http = httpx.AsyncClient(base_url=URL["gateway"] + "/api", headers={"X-Requested-With": "dashboard"}, timeout=30)

    async def json(self, method, path, body=None):
        r = await self.http.request(method, path, json=body)
        r.raise_for_status()
        return r.json()


async def mcp(key: str, work):
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {key}"}, timeout=30) as http:
        async with streamable_http_client(URL["gateway"] + "/mcp", http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await work(session)


async def names(key):
    return await mcp(key, lambda s: _list(s))


async def _list(s):
    return [t.name for t in (await s.list_tools()).tools]


async def call(key, tool, args):
    return await mcp(key, lambda s: s.call_tool(tool, args))


def text(result) -> str:
    return " ".join(getattr(c, "text", "") for c in result.content)


async def status_of(key) -> int:
    try:
        await names(key)
        return 200
    except BaseException as exc:  # noqa: BLE001
        while isinstance(exc, BaseExceptionGroup):
            exc = exc.exceptions[0]
        return exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else -1


async def wait_status(admin, slug, want, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s = next(s for s in await admin.json("GET", "/servers") if s["slug"] == slug)
        if s["status"] == want:
            return s
        await asyncio.sleep(0.5)
    return s


async def main() -> int:
    woo_api = fakes.fake_woocommerce()
    llm = fakes.fake_llm()
    servers = {name: await serve(app, PORTS[name]) for name, app in [
        ("woo_api", woo_api), ("llm", llm), ("woo", woo_mcp.app), ("notes", notes_mcp.app),
        ("slow", fakes.slow_mcp()), ("gateway", gateway_app)]}

    admin = Dashboard()
    await admin.json("POST", "/auth/login", {"email": "admin@example.com", "password": ADMIN_PASSWORD})
    for slug in ("woo", "notes", "slow"):
        await wait_status(admin, slug, "connected")
    listed = await admin.json("GET", "/servers")
    check("seeded servers connected, WooCommerce first", [s["slug"] for s in listed] == ["woo", "notes", "slow"]
          and all(s["status"] == "connected" for s in listed))

    print("\n== a member joins and connects an MCP client with only the URL and a key")
    invite = await admin.json("POST", "/invites", {"email": "b@example.com", "role": "member"})
    member = Dashboard()
    await member.json("POST", "/join", {"token": invite["invite_url"].rsplit("/", 1)[1], "name": "B", "password": "b-password-123"})
    key = (await member.json("POST", "/keys", {"name": "e2e"}))["key"]
    tools = await names(key)
    check("tools of every enabled server are listed", {t.split("__")[0] for t in tools} == {"woo", "notes", "slow"}, f"{len(tools)} tools")
    check("collision: notes__search and slow__search are both listed", {"notes__search", "slow__search"} <= set(tools))
    r = await call(key, "woo__list_orders", {"period": "today"})
    check("woo__list_orders(today) is routed to woo and returns the order", not r.isError and '"id": 101' in text(r))
    await call(key, "notes__add_note", {"title": "hello", "body": "from e2e"})
    r = await call(key, "notes__search", {"query": "e2e"})
    check("notes__search is routed to notes, not to the other 'search'", not r.isError and "from e2e" in text(r))

    print("\n== tool permissions: members read orders, only admins create products or change order status")
    admin_key = (await admin.json("POST", "/keys", {"name": "e2e-admin"}))["key"]
    admin_tools = await names(admin_key)
    check("members aren't listed the admin-only tools", "woo__create_product" not in tools
          and "woo__update_order_status" not in tools and "woo__list_orders" in tools)
    check("admins are listed every tool", {"woo__create_product", "woo__update_order_status"} <= set(admin_tools))
    before = len(woo_api.state.woo["products"])
    r = await call(key, "woo__create_product", {"name": "Not allowed", "regular_price": "1.00"})
    check("a member calling an admin-only tool directly is refused",
          r.isError and "admins only" in text(r) and len(woo_api.state.woo["products"]) == before, text(r)[:60])
    woo = next(s for s in await member.json("GET", "/servers") if s["slug"] == "woo")
    check("the dashboard doesn't show members the admin-only tools", "woo__create_product" not in [t["name"] for t in woo["tools"]])
    woo_id = woo["id"]
    await admin.json("PATCH", f"/servers/{woo_id}", {"admin_only_tools": ["create_product"]})
    check("an admin opening a tool to members lists it for them at once", "woo__update_order_status" in await names(key))
    await admin.json("PATCH", f"/servers/{woo_id}", {"admin_only_tools": ["create_product", "update_order_status"]})
    check("and locking it again hides it", "woo__update_order_status" not in await names(key))

    print("\n== a slow upstream times out without holding up the others")
    started = time.monotonic()
    slow, fast = await asyncio.gather(call(key, "slow__search", {"query": "x"}), call(key, "woo__list_products", {}))
    slow_s = time.monotonic() - started
    check("slow call fails with a clear timeout error", slow.isError and "did not respond within 3s" in text(slow), text(slow)[:70])
    check("the timeout took about the configured 3 s", 2.5 < slow_s < 8, f"{slow_s:.1f} s")
    check("a call to another server at the same time succeeds", not fast.isError)
    check("the slow server stays listed (it answered health checks)", "slow__search" in await names(key))
    r = await call(key, "woo__get_order", {"order_id": 999})
    check("an upstream that answers with an error stays listed", r.isError and "woo__get_order" in await names(key), text(r)[:60])

    print("\n== upstream credentials never leave the gateway")
    blobs = [json.dumps(await c.json("GET", p)) for c in (admin, member) for p in ("/me", "/servers", "/keys", "/audit")]
    blobs += [json.dumps(await admin.json("GET", p)) for p in ("/users", "/invites")]
    blobs += [json.dumps(tools)]
    leaked = [s for s in (WOO_TOKEN, NOTES_TOKEN, "llm-secret-3333") if any(s in b for b in blobs)]
    check("no upstream credential or LLM key in any API response or the tool list", not leaked, f"{len(blobs)} responses")
    member_servers = await member.json("GET", "/servers")
    check("members don't see server URLs or auth settings", all("url" not in s and "auth_type" not in s for s in member_servers))
    check("members can't manage servers", (await member.http.post("/servers", json={"name": "x", "slug": "xx", "url": "http://x/mcp"})).status_code == 403)

    print("\n== SSRF guard: admins can't point the gateway at internal addresses")
    notes_port = PORTS["notes"]
    for label, url in [("cloud metadata", "http://169.254.169.254/latest/meta-data/"),
                       ("localhost (the notes server, by a name not on the list)", f"http://localhost:{notes_port}/mcp"),
                       ("127.0.0.1 written as one number", f"http://2130706433:{notes_port}/mcp"),
                       ("loopback inside an IPv6 address", f"http://[::ffff:127.0.0.1]:{notes_port}/mcp"),
                       ("a private network", "http://10.0.0.1/mcp")]:
        r = await admin.http.post("/servers", json={"name": "x", "slug": "ssrf-test", "url": url})
        check(f"refused: {label}", r.status_code == 400 and "internal address" in r.text, f"HTTP {r.status_code}")
    woo_url_before = next(s for s in await admin.json("GET", "/servers") if s["slug"] == "woo")["url"]
    r = await admin.http.patch(f"/servers/{woo_id}", json={"url": "http://169.254.169.254/mcp"})
    after = next(s for s in await admin.json("GET", "/servers") if s["slug"] == "woo")["url"]
    check("editing a server's URL to an internal address is refused too", r.status_code == 400 and after == woo_url_before)
    # A URL already in the database (or a name whose DNS changes later) is checked again before connecting.
    from app.db import Server, Session  # noqa: E402 - the gateway runs in this process
    async with Session() as db:
        db.add(Server(slug="sneaky", name="Sneaky", url=f"http://localhost:{notes_port}/mcp"))
        await db.commit()
    s = await wait_status(admin, "sneaky", "unreachable")
    check("an internal URL that got into the database is still never connected to", "blocked" in (s.get("last_error") or ""),
          (s.get("last_error") or "")[:70])
    check("and its tools are never listed", not any(t.startswith("sneaky__") for t in await names(admin_key)))
    await admin.json("DELETE", f"/servers/{s['id']}")

    print("\n== the chat agent acts as the signed-in user")
    reply = await admin.json("POST", "/chat", {"messages": [{"role": "user", "content": "Create a product called Linen Apron for 24.00"}]})
    check("chat created the product through the gateway", [s["tool"] for s in reply["steps"]] == ["woo__create_product"]
          and any(p["name"] == "Linen Apron" for p in woo_api.state.woo["products"]))
    check("the LLM was offered the same tools as the admin's MCP client", set(llm.state.seen["offered"]) == set(admin_tools))
    await member.json("POST", "/chat", {"messages": [{"role": "user", "content": "What orders came in today?"}]})
    check("a member's chat is offered the member's tools, without the admin-only ones", set(llm.state.seen["offered"]) == set(tools))

    print("\n== disabling a server, killing one, revoking a key")
    notes_id = next(s["id"] for s in listed if s["slug"] == "notes")
    await admin.json("PATCH", f"/servers/{notes_id}", {"enabled": False})
    check("disabled server's tools leave the MCP list", not any(t.startswith("notes__") for t in await names(key)))
    r = await call(key, "notes__list_notes", {})
    check("calling a disabled server's tool is refused", r.isError and "disabled" in text(r))
    await admin.json("POST", "/chat", {"messages": [{"role": "user", "content": "What orders came in today?"}]})
    check("the chat agent isn't offered the disabled server's tools", not any(t.startswith("notes__") for t in llm.state.seen["offered"]))
    await admin.json("PATCH", f"/servers/{notes_id}", {"enabled": True})
    await wait_status(admin, "notes", "connected")

    servers["notes"].should_exit = True  # kill the notes server
    await asyncio.sleep(1)
    r = await call(key, "notes__list_notes", {})
    check("calling a dead upstream fails with 'unreachable'", r.isError and "unreachable" in text(r), text(r)[:70])
    s = await wait_status(admin, "notes", "unreachable")
    check("the dashboard shows it Unreachable with a reason", s["status"] == "unreachable" and bool(s["last_error"]))
    check("the other servers keep working", not (await call(key, "woo__list_orders", {})).isError)

    key_id = (await member.json("GET", "/keys"))[0]["id"]
    await admin.json("DELETE", f"/keys/{key_id}")
    check("a revoked key is rejected with 401", await status_of(key) == 401)

    print("\n== the audit log")
    rows = await admin.json("GET", "/audit?limit=500")
    outcomes = {(r["user_email"], r["via"], r["outcome"]) for r in rows}
    for row in [("b@example.com", "mcp", "ok"), ("b@example.com", "mcp", "timeout"), ("b@example.com", "mcp", "error"),
                ("b@example.com", "mcp", "denied"), ("b@example.com", "mcp", "unreachable"),
                ("b@example.com", "mcp", "rejected"), ("admin@example.com", "chat", "ok")]:
        check(f"audit has {row[2]!r} under {row[0]} via {row[1]}", row in outcomes)
    check("the refused admin-only call is audited as denied under the member",
          any(r["user_email"] == "b@example.com" and r["tool"] == "create_product" and r["outcome"] == "denied"
              and r["detail"] == "admins only" for r in rows))
    timeout_row = next(r for r in rows if r["outcome"] == "timeout")
    check("audit rows carry server, tool and duration", timeout_row["server"] == "slow" and timeout_row["tool"] == "search"
          and timeout_row["duration_ms"] >= 2500, f"{timeout_row['duration_ms']} ms")
    mine = await member.json("GET", "/audit")
    check("a member sees only their own audit rows", mine and all(r["user_email"] == "b@example.com" for r in mine))

    for server in servers.values():
        server.should_exit = True
    await asyncio.gather(*TASKS, return_exceptions=True)  # let every server shut down before the loop closes
    print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
    return failures


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
