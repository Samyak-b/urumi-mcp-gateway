#!/usr/bin/env bash
# Proves "no hardcoded secrets in source code": instead of guessing with keyword searches, it takes every real
# secret value in use (generated passwords and tokens, the LLM key from .env) and checks that none of them appears
# in any tracked file or in any commit in git history. Then it checks that the secret files are git-ignored, that
# the charts ship every secret empty, and that nothing secret-shaped (gateway keys, Woo keys, private keys) is
# committed. Exit code 1 on any finding.
set -euo pipefail
cd "$(dirname "$0")/.."

fail=0
bad() { echo "FAIL  $*"; fail=1; }
ok() { echo "ok    $*"; }

# 1. Real secret values: never in the working tree's tracked files, never in history.
values=()
while IFS='=' read -r k v; do
  [[ $k =~ ^[A-Z_]+$ && -n $v ]] && values+=("$k=${v%$'\r'}")
done < <(cat deploy/secrets/generated.env 2>/dev/null; grep -E '^LLM_API_KEY=' .env 2>/dev/null || true)
commits=$(git rev-list --all)
for kv in "${values[@]}"; do
  name=${kv%%=*} value=${kv#*=}
  if git grep -qF -- "$value" 2>/dev/null; then bad "$name appears in a tracked file"; continue; fi
  if [[ -n $commits ]] && git grep -qF -- "$value" $commits 2>/dev/null; then bad "$name appears in git history"; continue; fi
done
ok "${#values[@]} real secret values checked: none in tracked files or in any of $(echo "$commits" | wc -w) commits"

# 2. The files holding them are git-ignored.
for f in .env deploy/secrets/generated.env deploy/secrets/platform.yaml; do
  git check-ignore -q "$f" && ok "$f is git-ignored" || bad "$f is NOT git-ignored"
done

# 3. Charts ship every value under `secrets:` empty; the real ones arrive from deploy/secrets/ at install time.
for f in charts/*/values*.yaml; do
  set_values=$(awk '/^secrets:/{s=1;next} /^[^ #]/{s=0} s && /^  [A-Za-z]+:/ && $2 != "\"\"" {print $1}' "$f")
  [[ -z $set_values ]] && ok "$f: no secret values" || bad "$f sets secrets: $set_values"
done

# 4. Secret-shaped strings anywhere in tracked files.
patterns='mcpgw_[A-Za-z0-9_-]{30,}|ck_[0-9a-f]{40}|cs_[0-9a-f]{40}|BEGIN [A-Z ]*PRIVATE KEY|sk-[A-Za-z0-9]{20,}'
if hits=$(git grep -nE "$patterns" -- . 2>/dev/null); then bad "secret-shaped strings found:"; echo "$hits"; else ok "no secret-shaped strings (gateway keys, Woo keys, private keys) in tracked files"; fi

exit $fail
