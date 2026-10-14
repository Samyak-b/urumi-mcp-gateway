"""The assignment's definition of done, run against the live cluster through the real Ingress.

    make smoke              (runs this in the gateway image, with kubectl and the kubeconfig mounted)
    SKIP_CHAT=1 make smoke  (skips the steps that call the LLM)

Two users: A is the admin, who uses the dashboard chat; B is a member invited by this run, who uses an MCP client
with nothing but the gateway URL and an API key. Every step prints PASS or FAIL; the exit code is the number of
failures. Each run creates its own member (smoke-<time>@example.com, disabled at the end), product, orders and
note, so it is safe to repeat, but it leaves them behind: run it before resetting for a demo, not after.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

DASH = "http://dashboard.localhost"
STORE = "http://store.localhost"
RUN = time.strftime("%H%M%S")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@example.com")
ADMIN_PASSWORD = os.environ["ADMIN_PASSWORD"]
SKIP_CHAT = os.environ.get("SKIP_CHAT") == "1"
failures = 0


def check(name: str, ok: bool, detail: str = "") -> bool:
    global failures
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""), flush=True)
    return ok


def section(title: str) -> None:
    print(f"\n== {title}", flush=True)


def kubectl(*args: str) -> str:
    return subprocess.run(["kubectl", *args], check=True, capture_output=True, text=True).stdout


def wc_order_in_admin(order_id: int) -> str:
    """The order as WooCommerce's own admin sees it (WooCommerce's order store, read inside the WordPress pod)."""
    php = (f'define("WP_USE_THEMES",false); require "/var/www/html/wp-load.php"; $o=wc_get_order({order_id}); '
           'echo $o ? $o->get_status()." ".$o->get_payment_method_title() : "missing";')
    return kubectl("-n", "store", "exec", "deploy/wordpress", "--", "php", "-r", php).strip()


# ---------------- the dashboard API as a signed-in user ----------------

class Dashboard:
    def __init__(self) -> None:
        self.http = httpx.Client(base_url=f"{DASH}/api", timeout=200, headers={"X-Requested-With": "dashboard"})

    def __call__(self, method: str, path: str, body: dict | None = None) -> httpx.Response:
        return self.http.request(method, path, json=body)

    def json(self, method: str, path: str, body: dict | None = None):
        r = self(method, path, body)
        r.raise_for_status()
        return r.json()

    def server(self, slug: str) -> dict:
        return next(s for s in self.json("GET", "/servers") if s["slug"] == slug)

    def wait_status(self, slug: str, want: str, timeout: float = 40) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            s = self.server(slug)
            if s["status"] == want or time.monotonic() > deadline:
                return s
            time.sleep(2)

    def chat(self, text: str) -> dict:
        return self.json("POST", "/chat", {"messages": [{"role": "user", "content": text}]})


# ---------------- the storefront, as a shopper ----------------

def checkout(product_name: str, qty: int = 1) -> int:
    """Add a product to the cart and check out with Cash on delivery, through the Store API (what the
    storefront's own checkout page calls)."""
    store = httpx.Client(base_url=f"{STORE}/wp-json/wc/store/v1", timeout=60)
    product = store.get("/products", params={"search": product_name}).json()[0]
    r = store.get("/cart")
    headers = {"Nonce": r.headers["Nonce"], "Cart-Token": r.headers["Cart-Token"]}
    store.post("/cart/add-item", json={"id": product["id"], "quantity": qty}, headers=headers).raise_for_status()
    addr = {"first_name": "Smoke", "last_name": f"Run {RUN}", "address_1": "1 Test St", "city": "Austin", "state": "TX",
            "postcode": "73301", "country": "US", "email": "smoke@example.com", "phone": "5550100"}
    r = store.post("/checkout", json={"billing_address": addr, "shipping_address": addr, "payment_method": "cod"},
                   headers=headers)
    r.raise_for_status()
    return r.json()["order_id"]


def on_storefront(product_name: str) -> bool:
    found = httpx.get(f"{STORE}/wp-json/wc/store/v1/products", params={"search": product_name}, timeout=30).json()
    return any(p["name"] == product_name for p in found)


# ---------------- an external MCP client, with only the URL and a key ----------------

async def mcp_session(key: str, work):
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {key}"}, timeout=90) as http:
        async with streamable_http_client(f"{DASH}/mcp", http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await work(session)


async def tool_names(key: str) -> set[str]:
    return await mcp_session(key, lambda s: _names(s))


async def _names(s):
    return {t.name for t in (await s.list_tools()).tools}


async def call(key: str, tool: str, args: dict):
    return await mcp_session(key, lambda s: s.call_tool(tool, args))


def text(result) -> str:
    return " ".join(getattr(c, "text", "") for c in result.content)


async def http_status(key: str) -> int:
    try:
        await tool_names(key)
        return 200
    except BaseException as exc:  # noqa: BLE001 - the MCP client wraps HTTP errors in exception groups
        while isinstance(exc, BaseExceptionGroup):
            exc = exc.exceptions[0]
        return exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else -1


# ---------------- the definition of done ----------------

def main() -> int:
    started = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    a = Dashboard()
    a.json("POST", "/auth/login", {"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    admin_id = a.json("GET", "/me")["user"]["id"]

    section("Store: storefront checkout with Cash on delivery, order visible in WooCommerce admin")
    order1 = checkout("Canvas Tote Bag")
    check("storefront checkout (COD)", order1 > 0, f"order #{order1}")
    check("order visible in WooCommerce admin", wc_order_in_admin(order1) == "processing Cash on delivery",
          wc_order_in_admin(order1))

    product = f"Smoke Item {RUN}"
    order2 = None
    section("User A (admin) in the dashboard chat")
    if SKIP_CHAT:
        print("skip  chat steps (SKIP_CHAT=1)")
    else:
        res = a.chat(f"Create a product called {product} for 12.34")
        tools = [s["tool"] for s in res["steps"]]
        check("chat created the product with woo__create_product", "woo__create_product" in tools, ", ".join(tools))
        check("the product appears on the storefront", on_storefront(product))
        order2 = checkout(product, qty=2)
        check("product ordered by hand on the storefront", order2 > 0, f"order #{order2}")
        res = a.chat("What orders came in today?")
        tools = [s["tool"] for s in res["steps"]]
        check("chat answered with woo__list_orders", "woo__list_orders" in tools, ", ".join(tools))
        check(f"the answer names order #{order2}", str(order2) in res["reply"], res["reply"][:120].replace("\n", " "))

    section("User B (member) joins and connects an MCP client with only the gateway URL and a key")
    b_email = f"smoke-{RUN}@example.com"
    invite = a.json("POST", "/invites", {"email": b_email, "role": "member"})
    b = Dashboard()
    token = invite["invite_url"].rsplit("/", 1)[1]
    b_user = b.json("POST", "/join", {"token": token, "name": f"Smoke B {RUN}", "password": "smoke-password-123"})
    check("B accepted the invite as a member", b_user["role"] == "member", b_email)
    key = b.json("POST", "/keys", {"name": "smoke MCP client"})["key"]
    enabled = {s["slug"] for s in a.json("GET", "/servers") if s["status"] == "connected"}
    names = asyncio.run(tool_names(key))
    prefixes = {n.split("__")[0] for n in names}
    check("B sees the tools of every enabled server", prefixes == enabled, f"{len(names)} tools from {sorted(prefixes)}")
    want = order2 or order1
    r = asyncio.run(call(key, "woo__list_orders", {"period": "today"}))
    check(f"B asks 'what orders came in today?' and gets #{want}", not r.isError and f'"id": {want}' in text(r))

    async def two_servers(s):
        orders = await s.call_tool("woo__list_orders", {"period": "today"})
        summary = f"{text(orders).count(chr(34) + 'id' + chr(34))} orders today (smoke {RUN})"
        note = await s.call_tool("notes__add_note", {"title": f"Smoke {RUN}", "body": summary, "tags": ["smoke"]})
        return orders, note
    orders, note = asyncio.run(mcp_session(key, two_servers))
    check("one task that needs two servers (woo, then notes) in one session", not orders.isError and not note.isError,
          text(note)[:60].replace("\n", " "))

    # Tool permissions: whatever an admin marked admin-only is neither listed for B nor callable by B. Called with
    # no arguments, so even a wrongly allowed call would only fail validation upstream, never change the store.
    locked = [t["name"] for s in a.json("GET", "/servers") for t in s["tools"] if t.get("admin_only")]
    if not locked:
        print("skip  tool permissions: no tool is admin-only (click one on the Servers page to lock it)")
    else:
        check("B isn't listed the admin-only tools", not set(locked) & names, ", ".join(locked))
        r = asyncio.run(call(key, locked[0], {}))
        check(f"B calling {locked[0]} directly is refused", r.isError and "admins only" in text(r), text(r)[:60])

    section("Team and control")
    notes = a.server("notes")
    a.json("PATCH", f"/servers/{notes['id']}", {"enabled": False})
    names = asyncio.run(tool_names(key))
    check("disabled server: its tools are gone for the MCP client", not any(n.startswith("notes__") for n in names),
          f"{len(names)} tools")
    r = asyncio.run(call(key, "notes__list_notes", {}))
    check("disabled server: calling its tool directly is refused", r.isError, text(r)[:70])
    if not SKIP_CHAT:
        res = a.chat("Using the team notes server, list our notes.")
        used = [s["tool"] for s in res["steps"]]
        check("disabled server: the chat agent no longer has its tools", not any(t.startswith("notes__") for t in used),
              ", ".join(used) or "no tools called")
    a.json("PATCH", f"/servers/{notes['id']}", {"enabled": True})
    check("re-enabled server is connected again", a.wait_status("notes", "connected")["status"] == "connected")

    kubectl("-n", "tools", "scale", "deploy/notes-mcp", "--replicas=0")
    try:
        s = a.wait_status("notes", "unreachable")
        check("killed upstream pod: dashboard shows it Unreachable with a reason", s["status"] == "unreachable",
              s.get("last_error") or "")
        r = asyncio.run(call(key, "woo__list_orders", {"period": "today"}))
        check("killed upstream pod: the other servers keep working", not r.isError)
        names = asyncio.run(tool_names(key))
        check("killed upstream pod: its tools leave the list", not any(n.startswith("notes__") for n in names))
    finally:
        kubectl("-n", "tools", "scale", "deploy/notes-mcp", "--replicas=1")
    check("restarted upstream recovers on its own", a.wait_status("notes", "connected", timeout=90)["status"] == "connected")

    key_id = next(k["id"] for k in b.json("GET", "/keys") if k["revoked_at"] is None)
    a.json("DELETE", f"/keys/{key_id}")
    check("revoked key: B's next call is rejected", asyncio.run(http_status(key)) == 401)

    section("Every call above is in the audit log, attributed to the right user")
    rows = [r for r in a.json("GET", "/audit?limit=500") if r["ts"][:19] >= started]
    b_rows = [r for r in rows if r["user_email"] == b_email]
    a_rows = [r for r in rows if r["user_email"] == ADMIN_EMAIL]
    has = lambda rs, **kw: any(all(r.get(k) == v for k, v in kw.items()) for r in rs)  # noqa: E731
    check("B's MCP calls are under B", has(b_rows, via="mcp", tool="list_orders", outcome="ok")
          and has(b_rows, via="mcp", tool="add_note", outcome="ok"), f"{len(b_rows)} rows")
    check("B's call to the disabled server is recorded as denied", has(b_rows, server="notes", outcome="denied"))
    check("B's attempt after revocation is recorded as rejected", has(b_rows, outcome="rejected"))
    if not SKIP_CHAT:
        check("A's chat calls are under A with source chat", has(a_rows, via="chat", tool="create_product", outcome="ok")
              and has(a_rows, via="chat", tool="list_orders", outcome="ok"), f"{len(a_rows)} rows")
    check("no row of this run is attributed to anyone else",
          all(r["user_email"] in (b_email, ADMIN_EMAIL) for r in rows), f"{len(rows)} rows since {started}Z")

    a.json("PATCH", f"/users/{b_user['id']}", {"disabled": True})  # tidy up: the smoke member can't sign in again
    print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}  (run {RUN}, admin id {admin_id})")
    return failures


if __name__ == "__main__":
    sys.exit(main())
