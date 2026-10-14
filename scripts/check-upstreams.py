"""Connect to every upstream MCP server the way the gateway will, and list its tools.

`make upstreams` runs this inside the cluster, in a pod labelled as the gateway in the platform namespace, so
it exercises the same network path and NetworkPolicies the gateway will use. It uses the official MCP client
SDK (Streamable HTTP), which handles both plain-JSON servers (ours) and SSE-streaming ones (DeepWiki).

Upstreams are passed as arguments: name=url, with an optional token in the env var TOKEN_<NAME>.
"""

import asyncio
import os
import sys
import time

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def check(name: str, url: str) -> bool:
    token = os.environ.get(f"TOKEN_{name.upper()}")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    start = time.monotonic()
    try:
        async with asyncio.timeout(20):
            async with streamablehttp_client(url, headers=headers) as (read, write, _):
                async with ClientSession(read, write) as session:
                    info = await session.initialize()
                    tools = (await session.list_tools()).tools
    except Exception as exc:  # report and move on: one bad upstream must not hide the others
        print(f"FAIL  {name:9} {url}\n      {type(exc).__name__}: {exc}")
        return False
    ms = (time.monotonic() - start) * 1000
    print(f"ok    {name:9} {info.serverInfo.name} {info.serverInfo.version}, {len(tools)} tools, {ms:.0f} ms")
    print("      " + ", ".join(t.name for t in tools))
    return True


async def main() -> int:
    results = [await check(*arg.split("=", 1)) for arg in sys.argv[1:]]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
