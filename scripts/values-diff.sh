#!/usr/bin/env bash
# What changes between the local cluster and a production-like VPS: renders every chart with values-local.yaml
# and with values-prod.yaml and prints the difference. The templates are the same for both, so this diff is the
# complete list of what production changes. Secret values are left out of the output.
set -euo pipefail
cd "$(dirname "$0")/.."
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

for c in store:store:store notes:notes-mcp:notes platform:platform:platform; do
  IFS=: read -r rel chart sec <<<"$c"
  for env in local prod; do
    helm template "$rel" "charts/$chart" -f "charts/$chart/values-$env.yaml" -f "deploy/secrets/$sec.yaml" \
      | grep -vE 'checksum/secret|^  [a-z-]+(password|secret|key|token|servers): ' >"$tmp/$env.yaml"
  done
  echo "== $chart  (< local   > prod)"
  changes=$(diff "$tmp/local.yaml" "$tmp/prod.yaml" | grep -E '^[<>]' | grep -vE '^[<>] *$' || true)
  echo "${changes:-   no differences}"
done
