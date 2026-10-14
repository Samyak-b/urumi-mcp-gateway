"""All configuration comes from environment variables, which the Helm chart sets from values and Secrets."""

from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "sqlite+aiosqlite:///./gateway.db"  # Postgres in the cluster; SQLite for local tests

    # Secrets. No usable defaults: the gateway refuses to start without them.
    session_secret: str = ""  # signs the dashboard's session cookies
    admin_email: str = "admin@example.com"
    admin_password: str = ""  # the first admin's password, used only when the users table is empty

    # Public URL of the dashboard host; used to show the MCP URL and to build invite links.
    public_url: str = "http://dashboard.localhost"
    cookie_secure: bool = False  # true in production, where the dashboard is served over HTTPS

    # Upstreams
    upstream_call_timeout_s: float = 30.0  # one tool call
    upstream_probe_timeout_s: float = 5.0  # one health check (connect + list tools)
    health_interval_s: float = 10.0

    # JSON list of servers to register on first start. It holds credentials, so it comes from a Secret.
    seed_servers: str = "[]"

    max_body_bytes: int = 1_000_000  # largest /mcp request body accepted


@lru_cache
def settings() -> Settings:
    return Settings()
