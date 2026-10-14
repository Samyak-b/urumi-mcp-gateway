"""A minimal MCP client for checking the gateway the way an external client (Claude Code, Cursor) uses it.

    python scripts/mcp-client.py URL KEY list
    python scripts/mcp-client.py URL KEY call TOOL '{"arg": "value"}'

It uses the official MCP client SDK over Streamable HTTP and sends only the gateway URL and the user's API key.
"""

import asyncio
import json
import sys

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main(url: str, key: str, action: str, *rest: str) -> int:
    try:
        async with httpx.AsyncClient(headers={"Authorization": f"Bearer {key}"}, timeout=60) as http:
            async with streamable_http_client(url, http_client=http) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    if action == "list":
                        tools = (await session.list_tools()).tools
                        print(f"{len(tools)} tools:")
                        for t in tools:
                            print(f"  {t.name:32} {(t.description or '')[:70]}")
                        return 0
                    tool, args = rest[0], json.loads(rest[1] if len(rest) > 1 else "{}")
                    result = await session.call_tool(tool, args)
                    text = " ".join(getattr(c, "text", "") for c in result.content)
                    print(("ERROR: " if result.isError else "") + text[:1500])
                    return 1 if result.isError else 0
    except BaseException as exc:  # noqa: BLE001 - show HTTP errors (401 etc.) plainly
        while isinstance(exc, BaseExceptionGroup):
            exc = exc.exceptions[0]
        if isinstance(exc, httpx.HTTPStatusError):
            print(f"HTTP {exc.response.status_code} {exc.response.reason_phrase}")  # streamed: no body to read
        else:
            print(f"{type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main(*sys.argv[1:])))
