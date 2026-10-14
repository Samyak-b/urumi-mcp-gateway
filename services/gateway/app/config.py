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
    # SSRF guard (ssrf.py): the only host names an upstream URL may use to reach an internal address. Comma-separated;
    # set from Helm values, so dashboard admins can't change it.
    internal_upstream_hosts: str = ""

    # JSON list of servers to register on first start. It holds credentials, so it comes from a Secret.
    seed_servers: str = "[]"

    max_body_bytes: int = 1_000_000  # largest /mcp request body accepted

    # Chat agent: any OpenAI-compatible /chat/completions endpoint with tool calling.
    llm_base_url: str = ""
    llm_api_key: str = ""  # from a Secret
    llm_model: str = ""
    llm_timeout_s: float = 60.0
    chat_max_steps: int = 8  # LLM round trips per message; stops a model that keeps calling tools
    store_timezone: str = "UTC"  # tells the model what "today" is


@lru_cache
def settings() -> Settings:
    return Settings()
