# WooCommerce hosting with a team MCP gateway

A WooCommerce store, a self-written WooCommerce MCP server, and a team MCP gateway with a React dashboard and a
chat agent, all on Kubernetes and deployed with Helm.

## Repository layout

| Path | What |
|---|---|
| `charts/` | One Helm chart per part, each with `values.yaml`, `values-local.yaml` and `values-prod.yaml` |
| `services/` | Source of the backend services (gateway, MCP servers) |
| `dashboard/` | React dashboard |
| `deploy/` | Cluster config and generated, git-ignored secrets |
| `scripts/` | Helper scripts used by the Makefile |
| `docs/` | System design and threat model |
| `tests/` | End-to-end and smoke tests |

## Local setup

Requirements: Windows with WSL2 (Ubuntu) and Docker Desktop with WSL integration on, or any Linux/macOS machine
with Docker. All commands run in the WSL shell, from the repository root.

```bash
make tools      # checks docker, k3d, kubectl, helm, openssl
```
