# System design and tradeoffs

## Components

```
                          namespace store                       namespace tools        namespace platform
Browser ─► Traefik ─┬─► WordPress + WooCommerce ◄── woo-mcp      notes-mcp              dashboard (React, nginx)
 (Ingress)          │        │                         ▲             ▲                  gateway ──► Postgres
                    │     MariaDB                      │             │                     │
                    └─► dashboard.localhost: / → dashboard, /api and /mcp → gateway        │
                                                       └─────────────┴──── gateway is an MCP client of each
                                                                           DeepWiki (public internet) ◄──┘
```

| Component | Responsibility | Where |
|---|---|---|
| WordPress + WooCommerce, MariaDB | The store. A Helm hook Job installs and configures it (WooCommerce, Cash on delivery, pages, products, the MCP server's API key) | `charts/store` |
| woo-mcp | Our MCP server for the store: `list_products`, `create_product`, `list_orders`, `get_order`, `update_order_status` | `services/woo-mcp` |
| notes-mcp | A second in-cluster upstream (a team notepad), used for two-server tasks and failure isolation | `services/notes-mcp`, `charts/notes-mcp` |
| DeepWiki | A public MCP server we don't control, the third upstream | `https://mcp.deepwiki.com/mcp` |
| gateway | Dashboard API (`/api`), the single MCP endpoint (`/mcp`), MCP client of every registered server, health checks, audit log, chat agent | `services/gateway` |
| Postgres | The gateway's state: users, API key hashes, invites, servers with their credentials and cached tools, the audit log | `charts/platform` |
| dashboard | React app: servers, chat, connect a client, team, audit log | `dashboard/` |

## Architecture choices

- **One gateway process for both front doors.** The MCP endpoint and the chat agent call the same function,
  `router.call_tool`, so which servers are enabled, how credentials are added and what is audited can't differ
  between an external client and the chat.
- **Stateless towards everyone.** `/mcp` runs the MCP SDK in stateless mode with plain JSON responses, the dashboard
  session is a signed cookie, and every upstream operation opens its own short-lived MCP session. Any gateway
  replica can answer any request, and nothing needs reconnecting after an upstream restarts. The cost is one extra
  round trip (`initialize`) per upstream call.
- **Tool lists come from a cache.** A background health check lists each server's tools every 10 s and stores them
  in Postgres with the status. `tools/list` reads that cache, so a slow or dead upstream can never make anyone's
  listing wait.
- **woo-mcp uses the WooCommerce REST API**, not SQL or WP-CLI: it is WooCommerce's supported interface (creating a
  product or changing an order status runs WooCommerce's own logic), it needs no exec access into the WordPress pod,
  and the MCP server holds only a scoped API key, never the database password. Details in `woo_client.py`.
- **Python everywhere on the server side**, with the official MCP SDK for both the servers and the gateway's client.
  Postgres rather than SQLite, so the gateway's state lives outside its pods.
- **One chart per part, one namespace per part**, so NetworkPolicies can separate them and each release can be
  upgraded or rolled back on its own.

## How a tool call travels

Claude Code asks for `woo__list_orders` with `{"period": "today"}`:

1. `POST https://<dashboard host>/mcp` with `Authorization: Bearer mcpgw_…` reaches Traefik, which routes `/mcp` to
   the gateway Service (`charts/platform/templates/ingress.yaml`).
2. `mcp_endpoint.mcp_asgi` hashes the key (SHA-256) and looks the hash up. An unknown key, a revoked key or a disabled
   user gets HTTP 401 before any MCP processing; the last two are audited as `rejected` under the key's owner.
3. The key's owner is put in a ContextVar and the request goes to the MCP SDK, which calls our `call_tool` handler.
4. `router.call_tool` splits the name on the first `__`: server `woo`, tool `list_orders`. It re-checks that the
   server exists, is enabled and has that tool (a client may still hold an old list), and that the tool isn't
   admin-only for a member, or answers `denied`.
5. `upstream.call` builds the request with **woo's own credential** from the database (the client's key goes no
   further), opens an MCP session to `http://woo-mcp.store.svc.cluster.local:8000/mcp` and calls the tool, all under a
   30 s deadline. Redirects are not followed, so the credential can't be carried to another host.
6. The NetworkPolicy in `store` admits the connection because it comes from a gateway pod in `platform`; woo-mcp
   checks the bearer token, turns `today` into midnight in the store's timezone, and calls
   `GET /wp-json/wc/v3/orders?after=…` on the WordPress Service with its API key.
7. The result goes back the same way. `audit.record` writes one row: user, `mcp`, `woo`, `list_orders`, duration,
   `ok`.

The chat agent starts at step 4: `chat.run` gets the user from the session cookie, offers the LLM the same tool
list `routable_tools(user)` gives `/mcp`, and runs each tool call the LLM asks for through `router.call_tool(user,
…, via="chat")`.

## Aggregation, naming and routing

- **Listing:** the tools of every server that is enabled and currently connected, each renamed
  `<slug>__<tool>` and with its description prefixed by the server's name (`router.routable_tools`).
- **Name collisions:** two servers may both have a `search` tool. Each server has a unique prefix (slug) of
  lowercase letters, digits and hyphens, never an underscore, so the first `__` in an exposed name always marks
  where the prefix ends: `notes__search` and `slow__search` route to different servers. Names are reduced to the
  characters clients accept and to 64 characters (a long name gets a 6-character hash so it stays unique).
  `make test` checks two colliding `search` tools.
- **Routing:** by prefix to the server, then by exposed name to the upstream's real tool name.
- **Tool permissions:** an admin can mark any tool admin-only (table `admin_only_tools`, one row per server and
  tool; the Servers page toggles it). For a member, `routable_tools` leaves those tools out, so neither their MCP
  client nor their chat is offered them, and `call_tool` refuses a direct call (`denied`, "admins only"). The check
  is in the two functions every call already goes through, so the two front doors can't disagree. Per role and per
  tool: woo's `create_product` and `update_order_status` start admin-only (a seed setting), so members read orders
  but don't change the store. Per-user grants or per-argument rules (only your own orders) would need more than a
  list; the role check is where they would plug in.
- **Upstream descriptions are capped at 1,024 characters**: they are text from a server we don't control, and they
  go straight to the model (see THREAT_MODEL.md).

## Failure handling

| Situation | What happens |
|---|---|
| Upstream slow | Every call has a hard deadline (30 s, `upstreamCallTimeoutSeconds`), every health check 5 s. The caller gets a clear `timeout` error; calls to other servers run independently. `make test` shows a 3 s timeout while a parallel call to another server succeeds |
| Upstream dead | The connection fails: the call returns `unreachable`, and the server is marked Unreachable at once (passive health check), not only at the next check. Its tools leave every list; the dashboard shows the reason. When it comes back, the next health check (≤10 s) lists it again |
| Upstream answers with an error | Bad arguments, "not found", an HTTP error status: the server is alive, so its tools stay listed and the call is audited as `error` with the server's message |
| Health checks | Run concurrently, each under its own deadline, so one slow server can't delay the others' status |
| Server disabled | Its tools leave every new listing and direct calls are refused (`denied`). A client that is already connected keeps its old list until it lists again: the gateway is stateless and doesn't push `tools/list_changed` |
| LLM down or misconfigured | `/api/chat` answers HTTP 503 with the provider's message instead of a 500; the rest of the gateway is unaffected |
| Database briefly down | Readiness (`/readyz` pings the database) takes the pod out of the Service; liveness (`/healthz`) doesn't, so a database blip doesn't restart the gateway. On a fresh install the gateway waits up to 45 s for Postgres instead of crash-looping |
| Gateway bug in a call | Caught, audited as `error`, and answered with a readable error result |

Every outcome is one audit row: `ok`, `error`, `timeout`, `unreachable`, `denied`, `rejected`.

## Kubernetes layout

| | store | tools | platform |
|---|---|---|---|
| Workloads | MariaDB (StatefulSet), WordPress, woo-mcp, setup Job | notes-mcp | gateway, dashboard, Postgres (StatefulSet) |
| Secrets | `store-db`, `store-admin`, `store-mcp` | `notes-mcp` | `gateway-secrets`, `postgres` |
| Volumes | `data-mariadb-0`, `wordpress-data` (kept on uninstall) | none (notes are in memory by design) | `data-postgres-0` |
| Ingress | `store.localhost` → WordPress | none | `dashboard.localhost`: `/` dashboard, `/api` and `/mcp` gateway |

- **Public vs internal:** only the two Ingress hosts are public. Every Service is ClusterIP. Each namespace denies
  all incoming traffic, then allows exactly: Traefik to WordPress, the dashboard and the gateway; woo-mcp to
  WordPress; WordPress and the setup Job to MariaDB; gateway pods to woo-mcp, notes-mcp and Postgres.
  `make isolation` shows the full matrix.
- **Probes, requests and limits** on every workload (`make workloads`). No pod gets a Kubernetes API token: nothing
  here talks to the Kubernetes API. Our four images run as uid 10001 with a read-only root filesystem.
- **Secrets:** `make secrets` generates every value once into git-ignored files, one values file per release holding
  only that release's secrets; the charts render them into Secrets and refuse to install with an empty one
  (`required`). `make secrets-check` proves no real value is in git.

## What changes for production

Only values (`make values-diff` prints the complete difference):

| | Local (k3d) | VPS (k3s) |
|---|---|---|
| DNS | `*.localhost`, resolved by the browser | real records, e.g. `store.example.com`, `app.example.com` |
| Ingress | Traefik, plain HTTP | Traefik with TLS from cert-manager (`deploy/prod/cluster-issuer.yaml`); the charts admit the ACME solver through their NetworkPolicies |
| Storage | the cluster's default class (local-path), 1–2 Gi | local-path set explicitly, 5–10 Gi; a cloud or replicated class is a values change |
| Images | built locally, copied in with `k3d image import` | pulled from a registry (`ghcr.io/samyak-b/*`) |
| Cookies | `COOKIE_SECURE=false` | HTTPS-only session cookie |
| Secrets | generated files on the developer's machine | the same generator; for a team, Sealed Secrets or External Secrets |

## Tradeoffs and known limits

- **Upstream credentials are stored in plain text in Postgres.** They are never returned by the API or sent to a
  client, and only the gateway can reach Postgres, but a database dump would show them. Encrypting them at rest is
  the first hardening step.
- **No SSRF guard yet:** an admin can register any URL, including in-cluster addresses. Admins are trusted in this
  version; THREAT_MODEL.md discusses the consequence.
- **One gateway replica.** A request that arrives during a rolling update can fail briefly. Running two replicas
  needs no code change (the gateway is stateless) apart from a startup lock so two fresh pods don't both seed the
  database.
- **No schema migrations:** tables are created if missing. A schema change needs a migration tool (Alembic) before
  it can be rolled out over existing data.
- **Every replica runs its own health loop**: simple and leader-free, at the cost of probe traffic multiplied by
  the replica count.
- **Disabled servers and connected clients:** see the failure table; a client must list again.
- **Requests with an unknown key are not audited**: there is no user to attribute them to. Revoked keys are.
- **Outgoing traffic is not restricted**: the NetworkPolicies cover incoming traffic only.
- **WordPress, MariaDB and Postgres run as root** inside their containers (the images' defaults); our own images
  don't.
- **notes-mcp keeps notes in memory**, so restarting it clears them.
