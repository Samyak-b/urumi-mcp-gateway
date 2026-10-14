#!/usr/bin/env bash
# What is public and what is internal-only, shown live.
#
# 1. Public: the only ways in from outside the cluster are the Ingress hosts below (through Traefik). Every
#    Service is ClusterIP: none is a NodePort or LoadBalancer of its own.
# 2. Internal: from four places inside the cluster, a throwaway pod tries a TCP connection to every internal
#    service. "open" = connected, "blocked" = dropped by a NetworkPolicy. Only the paths the design needs are open,
#    e.g. only a pod labelled gateway, in the platform namespace, reaches the MCP servers and Postgres.
set -euo pipefail

echo "== Public entry points (Ingress, through Traefik)"
kubectl get ingress -A -o jsonpath='{range .items[*]}{range .spec.rules[*]}{"   "}{.host}{": "}{range .http.paths[*]}{.path}{" -> "}{.backend.service.name}{"   "}{end}{"\n"}{end}{end}'
exposed=$(kubectl get svc -A -o jsonpath='{range .items[?(@.spec.type!="ClusterIP")]}{.metadata.namespace}/{.metadata.name} {end}' \
  | tr ' ' '\n' | grep -E '^(store|tools|platform)/' || true)
echo "   Services that bypass the Ingress (NodePort/LoadBalancer): ${exposed:-none}"

TARGETS=(
  "woo-mcp    woo-mcp.store.svc.cluster.local 8000"
  "notes-mcp  notes-mcp.tools.svc.cluster.local 8000"
  "wordpress  wordpress.store.svc.cluster.local 80"
  "mariadb    mariadb.store.svc.cluster.local 3306"
  "gateway    gateway.platform.svc.cluster.local 8000"
  "postgres   postgres.platform.svc.cluster.local 5432"
)
script='sleep 3'
for t in "${TARGETS[@]}"; do
  set -- $t
  script+="; printf '%-10s ' $1; nc -z -w 3 $2 $3 && echo open || echo blocked"
done

# from: namespace, labels, description
probe() {
  echo
  echo "== From $3"
  kubectl run "np-$RANDOM" -n "$1" --rm -i --restart=Never --quiet --labels="$2" \
    --image=busybox:1.36 --command -- sh -c "$script" 2>/dev/null | sed 's/^/   /'
}
kubectl create namespace platform --dry-run=client -o yaml | kubectl apply -f - >/dev/null
probe default app=probe "an unrelated pod in the default namespace"
probe store app=probe "a non-gateway pod inside the store namespace"
probe platform app=probe "a non-gateway pod inside the platform namespace"
probe platform app.kubernetes.io/name=gateway "a pod labelled gateway in the platform namespace"
