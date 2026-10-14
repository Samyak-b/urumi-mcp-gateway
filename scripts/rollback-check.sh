#!/usr/bin/env bash
# Upgrades and rollback, shown live on the local cluster. It ends on the same revisions' configuration it started
# from (as new revision numbers), so nothing is left changed.
#
# 1. A broken upgrade: notes-mcp with an image that doesn't exist. --rollback-on-failure (as in `make deploy`)
#    puts the release back on its last good revision by itself, and the old pod keeps serving the whole time.
# 2. A manual rollback of the store (WordPress + woo-mcp) and the platform (gateway + dashboard) to an earlier
#    revision, then forward again. The data is counted before and after: orders and products in MariaDB, users,
#    servers and audit rows in Postgres; none may go down. A rollback replaces Kubernetes objects; it never touches
#    the volumes.
set -euo pipefail

NODE=k3d-urumi-server-0
TIMEOUT=10m
declare -A NS=([store]=store [notes]=tools [platform]=platform)

[ "$(kubectl config current-context)" = k3d-urumi ] || { echo "run this against the local cluster (k3d-urumi)"; exit 1; }

deployed() {  # the revision that is live now
  helm history "$1" -n "${NS[$1]}" -o json | python3 -c \
    'import json,sys; print([r["revision"] for r in json.load(sys.stdin) if r["status"] == "deployed"][-1])'
}
earlier() {  # the revisions before $2 that installed cleanly, newest first
  helm history "$1" -n "${NS[$1]}" -o json | python3 -c \
    'import json,sys; print(*[r["revision"] for r in reversed(json.load(sys.stdin)) if r["revision"] < int(sys.argv[1]) and r["status"] == "superseded"])' "$2"
}
images() {  # our own images at a revision (other images are pulled from their registries)
  helm get manifest "$1" -n "${NS[$1]}" --revision "$2" | sed -n 's/^ *image: *"\{0,1\}\(urumi\/[^" ]*\)"\{0,1\}/\1/p' | sort -u | tr '\n' ' '
}
in_cluster() {  # local images can't be pulled, so a rollback target must still be in the node's image store
  for img in $1; do docker exec "$NODE" crictl inspecti -q "docker.io/$img" >/dev/null 2>&1 || return 1; done
}
counts() {
  local wp pg
  wp=$(kubectl -n store exec mariadb-0 -- sh -c 'mariadb -uroot -p"$MARIADB_ROOT_PASSWORD" -N -B wordpress -e "select (select count(*) from wp_posts where post_type=\"shop_order\"), (select count(*) from wp_posts where post_type=\"product\")"')
  pg=$(kubectl -n platform exec postgres-0 -- psql -U gateway -d gateway -tA -F' ' -c \
    "select (select count(*) from users), (select count(*) from servers), (select count(*) from audit_log)")
  set -- $wp $pg
  echo "orders=$1 products=$2 users=$3 servers=$4 audit_rows=$5"
}
serving() {  # the public entry points answer, and the gateway sees woo-mcp connected again (its health check)
  local store dash woo
  for _ in $(seq 1 12); do
    store=$(curl -s -o /dev/null -w '%{http_code}' --resolve store.localhost:80:127.0.0.1 http://store.localhost/ || true)
    dash=$(curl -s -o /dev/null -w '%{http_code}' --resolve dashboard.localhost:80:127.0.0.1 http://dashboard.localhost/api/me || true)
    woo=$(kubectl -n platform exec postgres-0 -- psql -U gateway -d gateway -tA -c "select status from servers where slug='woo'")
    [ "$store" = 200 ] && [ "$dash" = 401 ] && [ "$woo" = connected ] && break
    sleep 5
  done
  echo "   storefront HTTP $store, dashboard API HTTP $dash (401 = up, not signed in), gateway sees woo: $woo"
}

echo "== 1. A broken upgrade rolls itself back"
pod_before=$(kubectl -n tools get pods -l app.kubernetes.io/name=notes-mcp -o name)
echo "   notes is at revision $(deployed notes), pod $pod_before"
echo "   upgrading notes-mcp to image urumi/notes-mcp:does-not-exist (60 s timeout)..."
if helm upgrade notes charts/notes-mcp -n tools --reuse-values --set image=urumi/notes-mcp:does-not-exist \
     --wait --rollback-on-failure --timeout 60s >/dev/null 2>&1; then
  echo "   FAIL: the broken upgrade was accepted"; exit 1
fi
helm history notes -n tools --max 3 | cut -c1-120 | sed 's/^/   /'
pod_after=$(kubectl -n tools get pods -l app.kubernetes.io/name=notes-mcp -o name)
[ "$pod_before" = "$pod_after" ] && echo "   OK: same pod before and after ($pod_after): it never stopped serving" \
  || echo "   note: the pod changed ($pod_before -> $pod_after)"

echo
echo "== 2. Roll back the store and the platform, then forward again"
before=$(counts)
echo "   data before: $before"
for rel in store platform; do
  start=$(deployed "$rel"); now=$(images "$rel" "$start"); target=""
  # Prefer the newest earlier revision that ran different images (a real code rollback), else the previous one.
  for r in $(earlier "$rel" "$start"); do
    in_cluster "$(images "$rel" "$r")" || continue
    [ -z "$target" ] && target=$r
    if [ "$(images "$rel" "$r")" != "$now" ]; then target=$r; break; fi
  done
  [ -n "$target" ] || { echo "   $rel: no earlier revision whose images are still in the cluster"; continue; }
  echo
  echo "   $rel: revision $start ($now) -> back to $target ($(images "$rel" "$target"))"
  helm rollback "$rel" "$target" -n "${NS[$rel]}" --wait --timeout "$TIMEOUT" >/dev/null
  serving
  echo "   $rel: forward again to revision $start's configuration"
  helm rollback "$rel" "$start" -n "${NS[$rel]}" --wait --timeout "$TIMEOUT" >/dev/null
  serving
  helm history "$rel" -n "${NS[$rel]}" --max 3 | cut -c1-100 | sed 's/^/   /'
done

echo
after=$(counts)
echo "   data after:  $after"
# The system stays live during the check, so rows may be added (an order, a tool call); none may be lost.
lost=$(python3 -c 'import sys; b, a = (dict(kv.split("=") for kv in s.split()) for s in sys.argv[1:]); print(" ".join(k for k in b if int(a[k]) < int(b[k])))' "$before" "$after")
[ -z "$lost" ] && echo "   OK: nothing was lost" || { echo "   FAIL: fewer $lost than before"; exit 1; }
