# Local Kubernetes workflow. Run from a WSL2 (Ubuntu) shell with Docker Desktop's WSL integration on.
#
#   make tools     check that the required tools are installed and Docker is running
#   make cluster   create the local k3d cluster (skipped if it exists) and switch kubectl to it
#   make secrets   generate passwords into deploy/secrets/ (git-ignored); existing ones are kept
#   make build     build the service images with a fresh tag (remembered in deploy/.last-tag)
#   make import    copy the built images into the k3d cluster
#   make lint      helm lint every chart with this environment's values
#   make deploy    helm upgrade --install every release (waits until ready)
#   make creds     print the logins (keep them off camera)
#   make status    pods, services, ingresses, volumes in the project namespaces
#   make isolation show which pods may reach the internal services (NetworkPolicies)
#   make logs-setup follow the store setup Job
#   make down      delete the local cluster (all data goes with it)
#
# ENV=prod switches to the charts' values-prod.yaml files.

SHELL := /bin/bash
.SHELLFLAGS := -euo pipefail -c
.DEFAULT_GOAL := help

CLUSTER := urumi
ENV     ?= local
SECRETS := deploy/secrets

TAG_FILE := deploy/.last-tag

# Every build gets a new tag. Reusing one tag ("dev") would not restart pods after a rebuild, because
# the Deployment spec would not change. `make deploy` uses the last built tag.
ifndef TAG
TAG := $(shell cat $(TAG_FILE) 2>/dev/null || echo none)
endif

# image-name:source-dir
IMAGES := woo-mcp:services/woo-mcp notes-mcp:services/notes-mcp

# Local builds: point the charts at the freshly imported images. Prod uses the registry images in values-prod.yaml.
ifeq ($(ENV),local)
STORE_IMAGES = --set mcp.image=urumi/woo-mcp:$(TAG)
NOTES_IMAGES = --set image=urumi/notes-mcp:$(TAG)
endif

HELM_DEPLOY := helm upgrade --install --create-namespace --wait --timeout 20m

.PHONY: help tools cluster secrets build import lint deploy creds status isolation logs-setup down

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

build:
	$(eval TAG := local-$(shell date +%s))
	@for pair in $(IMAGES); do \
	  name=$${pair%%:*}; dir=$${pair#*:}; \
	  echo "building urumi/$$name:$(TAG)"; \
	  docker build -q -t urumi/$$name:$(TAG) $$dir; \
	done
	echo $(TAG) > $(TAG_FILE)

# k3d nodes cannot see the host's Docker images; this copies them into the cluster's containerd.
import:
	k3d image import -c $(CLUSTER) $(foreach pair,$(IMAGES),urumi/$(firstword $(subst :, ,$(pair))):$(TAG))

lint:
	helm lint charts/store     -f charts/store/values-$(ENV).yaml     -f $(SECRETS)/store.yaml
	helm lint charts/notes-mcp -f charts/notes-mcp/values-$(ENV).yaml -f $(SECRETS)/notes.yaml

deploy:
	@[ "$(ENV)" != local ] || [ "$(TAG)" != none ] || { echo "no images built yet: run make build import first"; exit 1; }
	$(HELM_DEPLOY) store charts/store -n store \
	  -f charts/store/values-$(ENV).yaml -f $(SECRETS)/store.yaml $(STORE_IMAGES)
	$(HELM_DEPLOY) notes charts/notes-mcp -n tools \
	  -f charts/notes-mcp/values-$(ENV).yaml -f $(SECRETS)/notes.yaml $(NOTES_IMAGES)

creds:
	@source <(grep -E '^STORE_ADMIN_PASSWORD=' $(SECRETS)/generated.env); \
	echo "WordPress  http://store.localhost/wp-admin   admin / $$STORE_ADMIN_PASSWORD"

status:
	kubectl get pods,svc,ingress,pvc,networkpolicy -n store
	kubectl get pods,svc,networkpolicy -n tools

logs-setup:
	kubectl -n store logs job/store-setup -f

# Shows the NetworkPolicies at work: throwaway curl pods try to reach internal services. Only a pod
# labelled as the gateway, in the platform namespace, gets through to woo-mcp.
isolation:
	@kubectl create namespace platform --dry-run=client -o yaml | kubectl apply -f - >/dev/null
	@probe() { kubectl run np-$$RANDOM -n $$1 --rm -i --restart=Never --quiet --labels="$$2" --image=curlimages/curl:8.11.1 \
	  --command -- sh -c "sleep 4; curl -s -m 5 -o /dev/null -w '%{http_code}' $$3 || true" 2>/dev/null | sed 's/^000$$/blocked/'; }; \
	echo "default ns           -> woo-mcp:   $$(probe default app=probe http://woo-mcp.store.svc.cluster.local:8000/healthz)"; \
	echo "store ns, other pod  -> woo-mcp:   $$(probe store app=probe http://woo-mcp.store.svc.cluster.local:8000/healthz)"; \
	echo "default ns           -> wordpress: $$(probe default app=probe http://wordpress.store.svc.cluster.local/)"; \
	echo "default ns           -> notes-mcp: $$(probe default app=probe http://notes-mcp.tools.svc.cluster.local:8000/healthz)"; \
	echo "platform ns, gateway -> woo-mcp:   $$(probe platform app.kubernetes.io/name=gateway http://woo-mcp.store.svc.cluster.local:8000/healthz)"; \
	echo "platform ns, gateway -> notes-mcp: $$(probe platform app.kubernetes.io/name=gateway http://notes-mcp.tools.svc.cluster.local:8000/healthz)"
