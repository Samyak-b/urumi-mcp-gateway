# Local Kubernetes workflow. Run from a WSL2 (Ubuntu) shell with Docker Desktop's WSL integration on.
#
#   make tools     check that the required tools are installed and Docker is running

SHELL := /bin/bash
.SHELLFLAGS := -euo pipefail -c
.DEFAULT_GOAL := help

.PHONY: help tools

help:
	@grep -E '^#   make' Makefile | sed 's/^#   //'

tools:
	@missing=0; for t in docker k3d kubectl helm openssl; do \
	  if command -v $$t >/dev/null; then echo "ok       $$t"; else echo "MISSING  $$t"; missing=1; fi; \
	done; \
	docker info >/dev/null 2>&1 || { echo "docker is installed but not running (start Docker Desktop)"; missing=1; }; \
	exit $$missing
