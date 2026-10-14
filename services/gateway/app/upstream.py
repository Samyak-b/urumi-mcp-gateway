"""The gateway as an MCP client of each upstream server.

One short-lived MCP session per operation (a health check or a tool call):
    connect -> initialize -> list_tools / call_tool -> close
That keeps the gateway stateless towards upstreams: any gateway replica can make any call, and there is nothing to
reconnect after an upstream restarts. The price is one extra round trip (initialize) per call. Every operation runs
under a hard deadline, so a slow or dead upstream can only ever cost its own caller that much time.
"""

from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, AsyncIterator

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError
from mcp.types import CallToolResult

from .config import settings


class UpstreamError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        # "unreachable": could not connect, or the connection broke (the server is down)
        # "timeout":     no answer within the deadline
        # "error":       the server answered, but with an error (so it is alive)
        self.kind = kind


@dataclass
class UpstreamTarget:
    """Everything needed to reach one upstream, built from a Server row. The credential becomes a header here,
    on the gateway, and nowhere else."""

    slug: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_server(cls, s) -> "UpstreamTarget":
        headers: dict[str, str] = {}
        if s.credential:
            if s.auth_type == "bearer":
                headers["Authorization"] = f"Bearer {s.credential}"
            elif s.auth_type == "header" and s.auth_header:
                headers[s.auth_header] = s.credential
        return cls(slug=s.slug, url=s.url, headers=headers)


@asynccontextmanager
async def _session(t: UpstreamTarget, timeout: float) -> AsyncIterator[ClientSession]:
    # Our own httpx client, so we control the headers and timeout and never follow redirects (a redirect could
    # carry the credential header to another host).
    async with httpx.AsyncClient(headers=t.headers, timeout=httpx.Timeout(timeout), follow_redirects=False) as http:
        async with streamable_http_client(t.url, http_client=http) as (read, write, _):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=timeout)) as session:
                await session.initialize()
                yield session


def _unwrap(exc: BaseException) -> BaseException:
    """The MCP client runs inside task groups, so errors arrive wrapped in ExceptionGroups."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


_URL = re.compile(r"https?://\S+")


def _describe(exc: BaseException) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return f"HTTP {code} from the server" + (" (check its credential)" if code in (401, 403) else "")
    if isinstance(exc, httpx.ConnectError):
        return "connection refused or host down"
    text = exc.error.message if isinstance(exc, McpError) else f"{exc.__class__.__name__}: {exc}"
    # Members see these messages (tool results), so never pass on a URL: they aren't shown server addresses.
    return _URL.sub("<url>", text)[:300]


async def _run(t: UpstreamTarget, timeout: float, op):
    try:
        async with asyncio.timeout(timeout):
            async with _session(t, timeout) as s:
                return await op(s)
    except TimeoutError as exc:
        raise UpstreamError("timeout", f"'{t.slug}' did not respond within {timeout:g}s") from exc
    except BaseException as exc:  # noqa: BLE001 - includes ExceptionGroup
        inner = _unwrap(exc)
        if isinstance(inner, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
            raise
        if isinstance(inner, (TimeoutError, httpx.TimeoutException)):
            raise UpstreamError("timeout", f"'{t.slug}' did not respond within {timeout:g}s") from exc
        if isinstance(inner, (httpx.NetworkError, OSError)):
            raise UpstreamError("unreachable", f"'{t.slug}' is unreachable: {_describe(inner)}") from exc
        # It answered, but with an error (bad arguments, an HTTP error status, a malformed reply). It is alive.
        raise UpstreamError("error", f"'{t.slug}' returned an error: {_describe(inner)}") from exc


async def probe(t: UpstreamTarget) -> list[dict[str, Any]]:
    """Connect and list every tool (following pagination). Used by the health check."""

    async def op(s: ClientSession):
        tools, cursor = [], None
        while True:
            res = await s.list_tools(cursor=cursor) if cursor else await s.list_tools()
            tools += [tool.model_dump(mode="json", exclude_none=True) for tool in res.tools]
            if not (cursor := res.nextCursor):
                return tools

    return await _run(t, settings().upstream_probe_timeout_s, op)


async def call(t: UpstreamTarget, tool: str, arguments: dict[str, Any]) -> CallToolResult:
    return await _run(t, settings().upstream_call_timeout_s, lambda s: s.call_tool(tool, arguments))
