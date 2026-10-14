# WooCommerce hosting with a team MCP gateway

A WooCommerce store on Kubernetes, an MCP server we wrote for it, and a **team MCP gateway**: an admin connects
MCP servers once in a dashboard, and every team member then uses all of them through **one MCP endpoint** (for
Claude Code, Cursor, MCP Inspector) and through a **chat agent** in the dashboard. Everything runs in the cluster
and is deployed with Helm; the same charts run locally (k3d) and on a VPS (k3s) with only values changing.

```
Browser ──► Traefik (Ingress)
              ├─ store.localhost ─────► WordPress + WooCommerce ──► MariaDB          namespace store
              └─ dashboard.localhost ─► dashboard (React)                            namespace platform
                                   ├─► gateway  /api (dashboard), /mcp (MCP clients) ──► Postgres
                                   │      │ MCP client of every registered server
                                   │      ├──► woo-mcp ──► WooCommerce REST API       namespace store
                                   │      ├──► notes-mcp                              namespace tools
                                   │      └──► DeepWiki (public internet)
Claude Code / Cursor ──────────────┘   one URL + the user's own API key
```

| Doc | What's in it |
|---|---|
| [docs/DESIGN.md](docs/DESIGN.md) | System design and tradeoffs: components, how a tool call travels, aggregation, routing, failure handling, what changes for production |
| [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md) | Shared upstream credentials, why client tokens aren't passed through, untrusted upstreams, what one member can do to another |

## Repository layout

| Path | What |
|---|---|
| `services/gateway` | The gateway: dashboard API, the `/mcp` endpoint, routing, health checks, audit log, chat agent (Python, FastAPI, MCP SDK) |
| `services/woo-mcp` | Our WooCommerce MCP server (Python, MCP SDK) |
| `services/notes-mcp` | A small team-notes MCP server, the second in-cluster upstream |
| `dashboard/` | React dashboard (Vite), served by a non-root nginx |
| `charts/store`, `charts/notes-mcp`, `charts/platform` | One Helm chart per part, each with `values.yaml`, `values-local.yaml`, `values-prod.yaml` |
| `deploy/` | k3d cluster config, the production ClusterIssuer, generated git-ignored secrets |
| `scripts/` | Secret generation and the checks behind the `make` targets |
| `tests/` | `test_e2e.py` (no cluster needed) and `smoke_cluster.py` (live cluster) |

## Local setup

**You need:** Docker (on Windows: Docker Desktop with WSL integration, and run everything in the WSL Ubuntu shell),
[k3d](https://k3d.io), [kubectl](https://kubernetes.io/docs/tasks/tools/), [Helm](https://helm.sh/docs/intro/install/)
and `make`. About 4 GB of free memory for the cluster.

```bash
cp .env.example .env     # fill in the LLM settings for the chat agent (optional) and the admin email
make up                  # cluster, secrets, images, deploy: about 10 minutes the first time
make creds               # the dashboard and WordPress admin logins
```

Then open **http://store.localhost** (the shop) and **http://dashboard.localhost** (the gateway). `make help`
lists every command. After a code change: `make build import deploy`. To start over: `make down && make up`.

What `make up` does, step by step:

| Step | What happens |
|---|---|
| `make cluster` | Creates the k3d cluster `urumi`: k3s in Docker, with Traefik published on ports 80/443 |
| `make secrets` | Generates every password and token into `deploy/secrets/` (git-ignored), once; re-running keeps them |
| `make build import` | Builds the four images with a fresh tag and copies them into the cluster |
| `make deploy` | `helm upgrade --install` of the `store`, `notes` and `platform` releases, waiting until they are ready |

The store installs itself: a Helm hook Job installs WordPress and WooCommerce, enables Cash on delivery, creates
the shop pages and three products, and registers the MCP server's API key. It runs again on every upgrade and
changes nothing that is already right.

### Local domains

`store.localhost` and `dashboard.localhost` need no setup: browsers resolve every `*.localhost` name to the local
machine (RFC 6761), k3d publishes Traefik on ports 80 and 443, and Traefik routes by host name. For tools that
don't resolve `*.localhost` themselves: `curl --resolve store.localhost:80:127.0.0.1 http://store.localhost`.

## Using it

**Place an order.** Open http://store.localhost, add a product to the cart, check out and choose **Cash on
delivery**. The order appears in http://store.localhost/wp-admin under WooCommerce → Orders (login from
`make creds`).

**Invite a teammate.** In the dashboard, **Team** → invite an email as member and send them the link. They choose a
password, and can then create their own API key under **Connect a client**.

**Connect an external MCP client** with only the gateway URL and your key (the Connect page shows these lines with
your key filled in):

```bash
# Claude Code
claude mcp add --transport http team-gateway http://dashboard.localhost/mcp --header "Authorization: Bearer mcpgw_..."
```

```json
// Cursor: .cursor/mcp.json
{ "mcpServers": { "team-gateway": { "url": "http://dashboard.localhost/mcp",
                                    "headers": { "Authorization": "Bearer mcpgw_..." } } } }
```

MCP Inspector: `npx @modelcontextprotocol/inspector`, transport **Streamable HTTP**, URL
`http://dashboard.localhost/mcp`, header `Authorization: Bearer mcpgw_...`.

The client sees the tools of every enabled server, named `<server>__<tool>` (`woo__list_orders`,
`notes__add_note`, `deepwiki__ask_wiki_question`). It never needs the servers' own credentials.

**Use the chat agent.** Dashboard → **Chat**, then ask "What orders came in today?" or "Create a product called
Linen Apron for 24.00". The tools it called are shown above each answer, and every call is in the **Audit log**
under your name with source *Chat*. It needs `LLM_BASE_URL`, `LLM_MODEL` and `LLM_API_KEY` in `.env` (any
OpenAI-compatible endpoint with tool calling), then `make secrets deploy`.

**Manage servers.** Dashboard → **Servers**: status (Connected / Unreachable with the reason / Disabled) and tools
of every server; admins add a server by URL with an optional bearer token or custom header, edit, disable or
remove it.

## Checks and tests

| Command | What it shows |
|---|---|
| `make test` | End-to-end test without Kubernetes: real gateway and MCP servers, a fake WooCommerce, a slow server and a scripted LLM. Routing, name collisions, timeouts, dead upstreams, revoked keys, credentials never leaving the gateway, the audit log. Also runs in CI |
| `make smoke` | The assignment's definition of done with two users, against the live cluster: order, chat, external MCP client, revoke, disable, kill a pod, audit attribution. Leaves its test data behind, so run it before resetting for a demo |
| `make isolation` | What is public (the two Ingress hosts) and that every internal service, databases included, is reachable only from the pods that need it |
| `make workloads` | Every workload has readiness and liveness probes, requests and limits |
| `make secrets-check` | No real secret value is in any tracked file or in git history; every chart ships its secrets empty |
| `make values-diff` | Everything that changes between local and production, from the rendered charts |
| `make upstreams` | Each upstream answers `tools/list`, checked from a gateway-labelled pod |

## Production-like VPS (k3s)

The same charts, with `values-prod.yaml` instead of `values-local.yaml`. `make values-diff` shows exactly what that
changes: hosts and URLs, TLS from cert-manager, storage class and sizes, registry images, secure cookies. This
version has been linted and rendered with the production values but not yet run on a VPS.

1. **A VPS** with Ubuntu, 2 vCPUs and 4 GB of RAM or more, ports 80 and 443 open, and two DNS records pointing at it,
   e.g. `store.example.com` and `app.example.com`.
2. **k3s** (it brings Traefik and the local-path storage class), then copy its kubeconfig to your machine:
   ```bash
   curl -sfL https://get.k3s.io | sh -
   sudo cat /etc/rancher/k3s/k3s.yaml     # save as ~/.kube/vps.yaml, replacing 127.0.0.1 with the VPS address
   ```
3. **cert-manager** and the Let's Encrypt issuer (put your email in `deploy/prod/cluster-issuer.yaml` first):
   ```bash
   export KUBECONFIG=~/.kube/vps.yaml
   helm install cert-manager cert-manager --repo https://charts.jetstack.io -n cert-manager --create-namespace --set crds.enabled=true
   kubectl apply -f deploy/prod/cluster-issuer.yaml
   ```
4. **Images** in a registry the VPS can pull from. The prod values point at `ghcr.io/samyak-b/*:0.1.0`:
   ```bash
   for pair in gateway:services/gateway woo-mcp:services/woo-mcp notes-mcp:services/notes-mcp dashboard:dashboard; do
     docker build -t ghcr.io/samyak-b/${pair%%:*}:0.1.0 ${pair#*:} && docker push ghcr.io/samyak-b/${pair%%:*}:0.1.0
   done
   ```
   Make the packages public on GitHub (the charts don't use an image pull secret), and build on, or for, the VPS's
   CPU architecture.
5. **Your domains** in `charts/store/values-prod.yaml` (`store.host`, `store.url`) and
   `charts/platform/values-prod.yaml` (`host`, `publicUrl`).
6. **Secrets and deploy**, from your machine:
   ```bash
   make secrets                                   # the same generator; keep deploy/secrets/ safe
   KUBECONFIG=~/.kube/vps.yaml make lint deploy ENV=prod
   ```

What differs from local, and why: real hosts instead of `*.localhost`; TLS certificates from Let's Encrypt (the
charts also let the ACME solver through their default-deny NetworkPolicies); the session cookie is HTTPS-only;
bigger volumes on k3s's local-path class (a cloud or replicated storage class is a values change); images from a
registry instead of `k3d image import`. Secrets come from the same generated files; a larger team would keep them
in Sealed Secrets or an External Secrets operator instead of files on one machine.

## Upgrades and rollback

Every `make build` gives the images a new tag, so `make deploy` changes the Deployments and Kubernetes rolls the
pods. Each `helm upgrade` is a numbered revision:

```bash
helm history platform -n platform            # the revisions of a release
helm rollback platform <revision> -n platform  # back to an earlier one; `make deploy` goes forward again
```

The store's data survives upgrades and rollbacks: MariaDB and WordPress keep their volumes, the WordPress volume
is kept even by `helm uninstall`, and the setup Job is safe to re-run. The gateway creates its tables if they are
missing but has no schema migrations yet (see DESIGN.md), so a release that changed the schema should not be rolled
back past.

## Bonus and stand-out items

None are attempted in this version yet; the core comes first. This section will list each one and where to find it.
