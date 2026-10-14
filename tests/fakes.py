"""Stand-ins for the parts the end-to-end test does not run for real: WooCommerce, a slow MCP server and the LLM."""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

# ---------------- WooCommerce REST API (wc/v3), just enough for woo-mcp ----------------

WOO_KEY, WOO_SECRET = "ck_test", "cs_test"


def fake_woocommerce() -> Starlette:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    state = {
        "products": [{"id": 1, "name": "Canvas Tote Bag", "status": "publish", "price": "18.00", "regular_price": "18.00",
                      "sku": "", "stock_status": "instock", "stock_quantity": None, "permalink": "https://wordpress/product/canvas-tote-bag/"}],
        "orders": [{"id": 101, "status": "processing", "total": "18.00", "currency": "USD", "date_created": now,
                    "date_created_gmt": now, "payment_method_title": "Cash on delivery", "shipping_total": "0.00",
                    "discount_total": "0.00", "customer_note": "",
                    "billing": {"first_name": "Test", "last_name": "Buyer", "email": "buyer@example.com"},
                    "line_items": [{"name": "Canvas Tote Bag", "quantity": 1, "total": "18.00"}]}],
        "notes": [],
    }

    def authorised(request: Request) -> bool:
        expected = "Basic " + base64.b64encode(f"{WOO_KEY}:{WOO_SECRET}".encode()).decode()
        return request.headers.get("authorization") == expected

    async def products(request: Request):
        if not authorised(request):
            return JSONResponse({"code": "woocommerce_rest_cannot_view", "message": "no"}, 401)
        if request.method == "POST":
            body = await request.json()
            p = {"id": len(state["products"]) + 1, "name": body["name"], "status": body.get("status", "publish"),
                 "price": body["regular_price"], "regular_price": body["regular_price"], "sku": body.get("sku", ""),
                 "stock_status": "instock", "stock_quantity": body.get("stock_quantity"),
                 "permalink": f"https://wordpress/product/{body['name'].lower().replace(' ', '-')}/"}
            state["products"].append(p)
            return JSONResponse(p, 201)
        return JSONResponse(state["products"], headers={"X-WP-Total": str(len(state["products"]))})

    async def orders(request: Request):
        if not authorised(request):
            return JSONResponse({"code": "woocommerce_rest_cannot_view", "message": "no"}, 401)
        after = request.query_params.get("after")
        found = [o for o in state["orders"] if not after or o["date_created_gmt"] >= after]
        return JSONResponse(found, headers={"X-WP-Total": str(len(found))})

    async def order(request: Request):
        oid = int(request.path_params["oid"])
        o = next((o for o in state["orders"] if o["id"] == oid), None)
        if o is None:
            return JSONResponse({"code": "woocommerce_rest_shop_order_invalid_id", "message": "Invalid ID."}, 404)
        if request.method == "PUT":
            o["status"] = (await request.json())["status"]
        return JSONResponse(o)

    async def notes(request: Request):
        state["notes"].append(await request.json())
        return JSONResponse({"id": len(state["notes"])}, 201)

    app = Starlette(routes=[
        Route("/wp-json/wc/v3/products", products, methods=["GET", "POST"]),
        Route("/wp-json/wc/v3/orders", orders),
        Route("/wp-json/wc/v3/orders/{oid:int}", order, methods=["GET", "PUT"]),
        Route("/wp-json/wc/v3/orders/{oid:int}/notes", notes, methods=["POST"]),
    ])
    app.state.woo = state
    return app


# ---------------- a slow MCP server whose tool name collides with notes' "search" ----------------

def slow_mcp():
    mcp = FastMCP("slow", stateless_http=True, json_response=True,
                  transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))

    @mcp.tool()
    async def search(query: str) -> str:
        """Takes far longer than the gateway's call timeout."""
        await asyncio.sleep(30)
        return "too late"

    return mcp.streamable_http_app()


# ---------------- an OpenAI-compatible LLM that follows a script ----------------

def fake_llm() -> Starlette:
    """Answers like a model with tool calling: for a known question it asks for a tool, and once it has a tool
    result it replies in text. It records the tool names it was offered, so the test can check them."""
    seen: dict = {"offered": []}

    async def completions(request: Request):
        body = await request.json()
        seen["offered"] = [t["function"]["name"] for t in body.get("tools", [])]
        last = body["messages"][-1]
        if last["role"] == "tool":
            return reply({"role": "assistant", "content": f"Done. The tool said: {last['content'][:200]}"})
        text = last["content"]
        if text.startswith("Create a product called "):
            name, price = text.removeprefix("Create a product called ").rsplit(" for ", 1)
            return tool_call("woo__create_product", {"name": name, "regular_price": price})
        if "orders" in text and "today" in text:
            return tool_call("woo__list_orders", {"period": "today"})
        return reply({"role": "assistant", "content": "I can't help with that."})

    def tool_call(name, args):
        return reply({"role": "assistant", "content": "",
                      "tool_calls": [{"id": "call_1", "type": "function",
                                      "function": {"name": name, "arguments": json.dumps(args)}}]})

    def reply(message):
        return JSONResponse({"choices": [{"index": 0, "message": message, "finish_reason": "stop"}]})

    app = Starlette(routes=[Route("/v1/chat/completions", completions, methods=["POST"])])
    app.state.seen = seen
    return app
