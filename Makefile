# Local Kubernetes workflow. Run from a WSL2 (Ubuntu) shell with Docker Desktop's WSL integration on.
#
#   make tools     check that the required tools are installed and Docker is running
#   make cluster   create the local k3d cluster (skipped if it exists) and switch kubectl to it
#   make secrets   generate passwords into deploy/secrets/ (git-ignored); existing ones are kept
#   make down      delete the local cluster (all data goes with it)

SHELL := /bin/bash
.SHELLFLAGS := -euo pipefail -c
.DEFAULT_GOAL := help

CLUSTER := urumi

.PHONY: help tools cluster secrets down

help:
	@grep -E '^#   make' Makefile | sed 's/^#   //'

tools:
	@missing=0; for t in docker k3d kubectl helm openssl; do \
	  if command -v $$t >/dev/null; then echo "ok       $$t"; else echo "MISSING  $$t"; missing=1; fi; \
	done; \
	docker info >/dev/null 2>&1 || { echo "docker is installed but not running (start Docker Desktop)"; missing=1; }; \
	exit $$missing

cluster:
	@k3d cluster list $(CLUSTER) >/dev/null 2>&1 && echo "cluster $(CLUSTER) already exists" \
	  || k3d cluster create --config deploy/k3d-config.yaml
	kubectl config use-context k3d-$(CLUSTER)

down:
	k3d cluster delete $(CLUSTER)

secrets:
	bash scripts/gen-secrets.sh
