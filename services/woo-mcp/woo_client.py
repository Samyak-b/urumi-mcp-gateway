"""Small async client for the WooCommerce REST API (wc/v3).

Why the REST API, and not WP-CLI or SQL:
  * It is WooCommerce's supported, versioned interface. Creating a product or changing an order status
    runs WooCommerce's own logic (stock, totals, order notes, hooks). SQL would skip all of that, and
    WP-CLI would need exec access into the WordPress pod.
  * It authenticates with a consumer key/secret scoped to the API, so this server never holds the
    database password.

Why the X-Forwarded-Proto header:
  WooCommerce accepts the key/secret as HTTP Basic auth only when WordPress believes the request came in
  over HTTPS. Over plain HTTP it demands OAuth 1.0a signatures computed against the public site URL, which
  doesn't match the in-cluster address we call. The hop from this pod to WordPress stays inside the cluster
  and is locked down by NetworkPolicies, so we tell WordPress that TLS ended in front of it; the official
  WordPress image trusts this header. For outside traffic Traefik sets the header itself, so a browser
  cannot use it to fake anything.
"""

from __future__ import annotations

from typing import Any

import httpx


class WooError(Exception):
    """An error whose message is safe to show to a model and tells it what to do next."""


class WooClient:
    def __init__(self, base_url: str, consumer_key: str, consumer_secret: str, timeout: float = 15.0):
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/wp-json/wc/v3",
            auth=(consumer_key, consumer_secret),
            headers={"X-Forwarded-Proto": "https", "Accept": "application/json"},
            timeout=timeout,
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def request(
        self, method: str, path: str, *, params: dict | None = None, json: dict | None = None
    ) -> tuple[Any, httpx.Headers]:
        """Call the API and return (parsed JSON, response headers), or raise WooError."""
        try:
            resp = await self._http.request(method, path, params=params, json=json)
        except httpx.TimeoutException as exc:
            raise WooError("The store did not answer in time. Try again in a moment.") from exc
        except httpx.HTTPError as exc:
            raise WooError(f"Could not reach the store ({exc.__class__.__name__}).") from exc

        if resp.status_code >= 400:
            raise WooError(_explain(resp))
        try:
            return resp.json(), resp.headers
        except ValueError as exc:
            raise WooError(f"The store returned a non-JSON response (HTTP {resp.status_code}).") from exc


def _explain(resp: httpx.Response) -> str:
    """Turn a WooCommerce error response into one clear sentence."""
    try:
        body = resp.json()
        code, message = body.get("code", ""), body.get("message", "")
    except ValueError:
        code, message = "", resp.text[:200]
    if resp.status_code == 401:
        return "The store rejected the MCP server's API key. An admin must check the store-mcp Secret."
    if resp.status_code == 404 or code.endswith("invalid_id"):
        return f"Not found ({message or code}). Use a list tool to find a valid ID."
    if resp.status_code == 400:
        return f"The store rejected the input: {message or code}."
    return f"Store error HTTP {resp.status_code}: {message or code}."
