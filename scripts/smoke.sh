#!/usr/bin/env bash
# Checks a running Compose stack from outside, over real HTTP: the health
# endpoint, the built UI and its bundle, JSON 404s, 405s with Allow, and
# that the container runs unprivileged with a writable data volume.
set -euo pipefail
base_url="${BASE_URL:-http://localhost:8200}"

field() { python3 -c 'import json, sys; print(json.loads(sys.argv[1])[sys.argv[2]])' "$1" "$2"; }
status() { curl --silent --output /dev/null --write-out '%{http_code}' "$@"; }
header() { # header NAME CURL-ARGS...: one response header's value
  local name="$1"; shift
  curl --silent --output /dev/null --dump-header - "$@" | tr -d '\r' \
    | awk -v n="$(echo "$name" | tr 'A-Z' 'a-z')" -F': ' 'tolower($1) == n { print $2 }'
}
expect() { # expect NAME GOT WANT
  if [ "$2" != "$3" ]; then echo "FAIL $1: got $2, want $3" >&2; exit 1; fi
  echo "ok   $1: $2"
}

body="$(curl --fail --silent --show-error "$base_url/api/health")"
expect "health status" "$(field "$body" status)" "ok"
expect "health version" "$(field "$body" version)" "0.1.0"

index="$(curl --fail --silent --show-error "$base_url/")"
expect "UI index served" "$(echo "$index" | grep -c '<div id="root">')" "1"
expect "index not cached" "$(header cache-control "$base_url/")" "no-cache"
bundle="$(echo "$index" | grep -o '/assets/index-[A-Za-z0-9_-]*\.js' | head -1)"
expect "index names a hashed bundle" "$([ -n "$bundle" ] && echo yes || echo no)" "yes"
expect "bundle served" "$(status "$base_url$bundle")" "200"
expect "bundle type" "$(header content-type "$base_url$bundle" | cut -d';' -f1)" "text/javascript"

expect "unknown API path" "$(status "$base_url/api/nope")" "404"
expect "unknown API path is JSON" "$(field "$(curl --silent "$base_url/api/nope")" detail)" "Not Found"
expect "wrong method" "$(status -X POST "$base_url/api/health")" "405"
expect "wrong method Allow" "$(header allow -X POST "$base_url/api/health")" "GET"

expect "runs as uid" "$(docker compose exec -T logscope id -u)" "10001"
expect "data volume writable" "$(docker compose exec -T logscope sh -c 'touch /data/.probe && rm /data/.probe && echo yes')" "yes"
expect "container health" "$(docker inspect --format '{{.State.Health.Status}}' "$(docker compose ps -q logscope)")" "healthy"

echo "smoke test passed"
