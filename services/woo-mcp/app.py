"""WooCommerce MCP server.

Exposes one store's products and orders as MCP tools over Streamable HTTP, at /mcp.
It is internal to the cluster: a ClusterIP Service with no Ingress, a NetworkPolicy that admits only gateway
pods, and a bearer token the gateway must present (defence in depth, in case the label-based policy is
bypassed).

Tool names here are plain (list_products, ...). The gateway adds the server prefix (woo__list_products) when
it combines several servers into one list.
"""

from __future__ import annotations

import hmac
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from woo_client import WooClient

log = logging.getLogger("woo-mcp")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")

STORE_URL = os.environ.get("WOO_BASE_URL", "http://wordpress")
# Public storefront URL for the links we return. WordPress builds permalinks with https (we tell it the hop
# was TLS, see woo_client.py), so the real scheme and host are put back here.
STORE_PUBLIC_URL = os.environ.get("STORE_PUBLIC_URL", "").rstrip("/")
STORE_TZ = ZoneInfo(os.environ.get("STORE_TIMEZONE", "UTC"))
MCP_TOKEN = os.environ.get("MCP_TOKEN", "")

woo = WooClient(STORE_URL, os.environ.get("WOO_CONSUMER_KEY", ""), os.environ.get("WOO_CONSUMER_SECRET", ""))

mcp = FastMCP(
    "woocommerce",
    instructions="Manage one WooCommerce store: list and create products, list and inspect orders, change order status.",
    stateless_http=True,  # no per-client session state, so any replica can answer any request
    json_response=True,  # answer with plain JSON instead of an SSE stream; tool calls here are short
    # The SDK's DNS-rebinding protection only accepts Host: localhost. Inside the cluster we are called by
    # Service name, and the only caller is the gateway, not a browser, so it is turned off.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)

OrderStatus = Literal["pending", "processing", "on-hold", "completed", "cancelled", "refunded", "failed"]


# ---------- shaping: return what a model needs, not WooCommerce's full payload ----------

def _public_link(url: str | None) -> str | None:
    if not url or not STORE_PUBLIC_URL:
        return url
    public, link = urlsplit(STORE_PUBLIC_URL), urlsplit(url)
    return urlunsplit((public.scheme, public.netloc, link.path, link.query, link.fragment))


def _product(p: dict) -> dict:
    return {
        "id": p["id"],
        "name": p["name"],
        "status": p["status"],
        "price": p.get("price") or p.get("regular_price"),
        "sku": p.get("sku") or None,
        "stock_status": p.get("stock_status"),
        "stock_quantity": p.get("stock_quantity"),
        "permalink": _public_link(p.get("permalink")),
    }


def _order_summary(o: dict) -> dict:
    billing = o.get("billing") or {}
    return {
        "id": o["id"],
        "status": o["status"],
        "total": o["total"],
        "currency": o.get("currency"),
        "created": o.get("date_created"),
        "customer": " ".join(x for x in [billing.get("first_name"), billing.get("last_name")] if x) or None,
        "items": [{"name": li["name"], "quantity": li["quantity"], "total": li["total"]} for li in o.get("line_items", [])],
    }


def _window(period: str) -> tuple[datetime | None, datetime | None]:
    """Turn a named period into UTC bounds. "Today" starts at midnight in the store's timezone."""
    if period == "all":
        return None, None
    now = datetime.now(STORE_TZ)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start, end = {
        "today": (midnight, None),
        "yesterday": (midnight - timedelta(days=1), midnight),
        "last_7_days": (midnight - timedelta(days=6), None),
        "last_30_days": (midnight - timedelta(days=29), None),
    }[period]
    return (start.astimezone(timezone.utc) if start else None, end.astimezone(timezone.utc) if end else None)


def _iso(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%S")


def _page(items: list, headers, page: int, per_page: int) -> dict:
    total = int(headers.get("X-WP-Total", len(items)))
    return {"page": page, "total": total, "has_more": page * per_page < total}


# ---------- tools ----------

@mcp.tool(annotations=READ)
async def list_products(
    search: Annotated[str | None, Field(description="Text to match in the product name or description")] = None,
    status: Annotated[Literal["any", "publish", "draft", "private"], Field(description="Product status filter")] = "any",
    per_page: Annotated[int, Field(ge=1, le=100)] = 20,
    page: Annotated[int, Field(ge=1)] = 1,
) -> dict[str, Any]:
    """List products in the store, newest first, with id, name, price, stock and storefront link."""
    params: dict[str, Any] = {"per_page": per_page, "page": page, "status": status, "orderby": "date", "order": "desc"}
    if search:
        params["search"] = search
    data, headers = await woo.request("GET", "/products", params=params)
    return {"products": [_product(p) for p in data], **_page(data, headers, page, per_page)}


@mcp.tool(annotations=WRITE)
async def create_product(
    name: Annotated[str, Field(min_length=1, max_length=200, description="Product name shown on the storefront")],
    regular_price: Annotated[str, Field(pattern=r"^\d+(\.\d{1,2})?$", description="Price as a decimal string, e.g. '19.99'")],
    description: Annotated[str, Field(max_length=5000)] = "",
    sku: Annotated[str | None, Field(max_length=100)] = None,
    stock_quantity: Annotated[int | None, Field(ge=0, description="Set to track stock; leave out for unlimited")] = None,
    status: Annotated[Literal["publish", "draft"], Field(description="'publish' shows it on the storefront")] = "publish",
) -> dict[str, Any]:
    """Create a simple product. A published product appears on the storefront immediately."""
    body: dict[str, Any] = {"name": name, "type": "simple", "regular_price": regular_price, "description": description, "status": status}
    if sku:
        body["sku"] = sku
    if stock_quantity is not None:
        body["manage_stock"] = True
        body["stock_quantity"] = stock_quantity
    data, _ = await woo.request("POST", "/products", json=body)
    return _product(data)


@mcp.tool(annotations=READ)
async def list_orders(
    period: Annotated[
        Literal["today", "yesterday", "last_7_days", "last_30_days", "all"],
        Field(description="Time window in the store's timezone. Use 'today' for 'orders that came in today'."),
    ] = "all",
    status: Annotated[OrderStatus | Literal["any"], Field(description="Order status filter")] = "any",
    per_page: Annotated[int, Field(ge=1, le=100)] = 20,
    page: Annotated[int, Field(ge=1)] = 1,
) -> dict[str, Any]:
    """List orders, newest first, with status, total, customer name and items."""
    params: dict[str, Any] = {"per_page": per_page, "page": page, "status": status, "orderby": "date", "order": "desc"}
    start, end = _window(period)
    if start or end:
        params["dates_are_gmt"] = "true"
    if start:
        params["after"] = _iso(start)
    if end:
        params["before"] = _iso(end)
    data, headers = await woo.request("GET", "/orders", params=params)
    return {
        "period": period,
        "store_timezone": str(STORE_TZ),
        "orders": [_order_summary(o) for o in data],
        **_page(data, headers, page, per_page),
    }


@mcp.tool(annotations=READ)
async def get_order(order_id: Annotated[int, Field(ge=1, description="Order ID from list_orders")]) -> dict[str, Any]:
    """Get one order in detail: items, totals, payment method and customer contact."""
    o, _ = await woo.request("GET", f"/orders/{order_id}")
    billing = o.get("billing") or {}
    return {
        **_order_summary(o),
        "payment_method": o.get("payment_method_title"),
        "shipping_total": o.get("shipping_total"),
        "discount_total": o.get("discount_total"),
        "customer_email": billing.get("email") or None,
        "customer_note": o.get("customer_note") or None,
    }


@mcp.tool(annotations=WRITE)
async def update_order_status(
    order_id: Annotated[int, Field(ge=1)],
    status: Annotated[OrderStatus, Field(description="New status")],
    note: Annotated[str | None, Field(max_length=1000, description="Optional private note added to the order")] = None,
) -> dict[str, Any]:
    """Change an order's status (for example processing to completed), optionally adding a private note."""
    o, _ = await woo.request("PUT", f"/orders/{order_id}", json={"status": status})
    if note:
        await woo.request("POST", f"/orders/{order_id}/notes", json={"note": note, "customer_note": False})
    return {"id": o["id"], "status": o["status"], "note_added": bool(note)}


# ---------- HTTP app: health check and bearer auth in front of /mcp ----------

@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> JSONResponse:
    # Checks only this process. A store outage shows up as tool errors; restarting this pod would not fix it.
    return JSONResponse({"ok": True})


class BearerAuth:
    """ASGI middleware: /mcp requests must carry the shared token from the gateway."""

    def __init__(self, app, token: str):
        self.app, self.token = app, token.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/mcp") and self.token:
            auth = dict(scope["headers"]).get(b"authorization", b"")
            # Constant-time comparison: the response time does not reveal how much of the token matched.
            if not hmac.compare_digest(auth, b"Bearer " + self.token):
                return await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
        return await self.app(scope, receive, send)


def build_app():
    app = mcp.streamable_http_app()
    sdk_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(a):
        async with sdk_lifespan(a):  # starts the SDK's session manager
            yield
        await woo.aclose()

    app.router.lifespan_context = lifespan
    if not MCP_TOKEN:
        log.warning("MCP_TOKEN is empty: /mcp is unauthenticated (acceptable only in tests)")
    return BearerAuth(app, MCP_TOKEN)


app = build_app()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
