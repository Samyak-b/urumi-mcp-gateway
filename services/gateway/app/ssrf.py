"""SSRF guard: which addresses the gateway may connect to as an MCP client.

An admin types a server URL in the dashboard, and the gateway then sends requests to it from inside the cluster.
Without a check, that URL could reach what only the gateway's position in the network can reach: the cloud
metadata service (169.254.169.254, which hands out the node's cloud credentials), localhost, the Kubernetes API,
our own Postgres, or any other private service.

The rule: resolve the host name and look at every address it resolves to. Public addresses are allowed. Internal
ones (private, loopback, link-local, carrier-grade NAT, multicast, reserved) are refused, unless the host name is
in INTERNAL_UPSTREAM_HOSTS: the in-cluster MCP services the operator lists in the Helm values. That list can't be
changed from the dashboard, so an admin can't widen it.

Checked when a server is saved (a clear error for the admin) and again before every connection, health checks and
tool calls alike, so a name that starts resolving to an internal address later is still refused. Redirects are
never followed (upstream.py), so a public server can't bounce a request inwards either.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

from .config import settings


class BlockedURL(ValueError):
    """The URL may not be used for an upstream. The message is safe to show (no resolved addresses)."""


def allowed_internal_hosts() -> set[str]:
    return {h.strip().lower().rstrip(".") for h in settings().internal_upstream_hosts.split(",") if h.strip()}


def _internal(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped  # ::ffff:127.0.0.1 is 127.0.0.1
    return not ip.is_global or ip.is_multicast


async def check(url: str) -> None:
    """Raise BlockedURL if the gateway must not connect to `url`."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise BlockedURL("only http:// or https:// URLs with a host name are allowed")
    host = parts.hostname.lower().rstrip(".")
    if host in allowed_internal_hosts():
        return
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, ValueError):
        return  # a name that doesn't resolve can't reach anything; connecting fails with "unreachable"
    for *_, sockaddr in infos:
        if _internal(ipaddress.ip_address(sockaddr[0].split("%")[0])):
            raise BlockedURL(
                f"'{host}' points to an internal address (private, loopback, link-local or cloud metadata). "
                "Only the in-cluster MCP services listed in the gateway's internalUpstreamHosts value may be internal."
            )
