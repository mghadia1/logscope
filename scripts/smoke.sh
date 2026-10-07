#!/usr/bin/env bash
# Checks a running Compose stack from outside, over real HTTP: the health
# endpoint, the built UI and its bundle, JSON 404s, 405s with Allow, that
# the container runs unprivileged with a writable data volume, and that
# ingested events are searchable and survive both a clean restart and a
# SIGKILL of the container, and that queries (the language) work through
# the index.
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

# Milestone 1: ingest, search, restart, SIGKILL.
ingest() { curl --fail --silent --show-error -X POST --data-binary @- "$base_url/api/ingest"; }
search() { curl --fail --silent --show-error "$base_url/api/search?start=2026-10-06T00:00:00Z&end=2026-10-07T00:00:00Z"; }
messages() { python3 -c 'import json, sys; print(" ".join(e["event"]["msg"] for e in json.load(sys.stdin)["events"]))'; }
stat() { field "$(curl --fail --silent --show-error "$base_url/api/stats")" "$1"; }
wait_healthy() {
  for _ in $(seq 60); do
    [ "$(status "$base_url/api/health")" = 200 ] && return 0
    sleep 0.5
  done
  echo "FAIL the API did not come back" >&2; exit 1
}

result="$(ingest <<'NDJSON'
{"time": "2026-10-06T21:00:00Z", "service": "smoke", "msg": "first"}
not json
{"time": "2026-10-06T21:00:01Z", "service": "smoke", "msg": "second"}
NDJSON
)"
expect "ingest accepted" "$(field "$result" accepted)" "2"
expect "ingest rejected" "$(field "$result" rejected)" "1"
expect "rejected line number" "$(python3 -c 'import json, sys; print(json.loads(sys.argv[1])["errors"][0]["line"])' "$result")" "2"
expect "search, newest first" "$(search | messages)" "second first"

docker compose restart logscope >/dev/null 2>&1
wait_healthy
expect "after a clean restart" "$(search | messages)" "second first"
expect "flushed to a segment on shutdown" "$(stat segment_events)" "2"

ingest <<<'{"time": "2026-10-06T21:00:02Z", "msg": "third"}' >/dev/null
expect "third event only in memory and the WAL" "$(stat events_in_memory)" "1"
docker compose kill --signal SIGKILL logscope >/dev/null 2>&1
docker compose up --detach --wait logscope >/dev/null 2>&1
wait_healthy
expect "after SIGKILL, replayed from the WAL" "$(search | messages)" "third second first"
expect "replayed into memory, not a segment" "$(stat events_in_memory)" "1"

# Milestone 2: query terms through the index, and the same without it.
query() { # query Q [INDEX]: the messages found
  curl --fail --silent --show-error --get --data-urlencode "q=$1" \
    --data-urlencode "index=${2:-true}" "$base_url/api/search" | messages
}
expect "q=service:smoke (indexed segment)" "$(query service:smoke)" "second first"
expect "q=third (memtable)" "$(query third)" "third"
expect "q=service:smoke without the index" "$(query service:smoke false)" "second first"
expect "q=nothing-like-this" "$(query nothing-like-this)" ""
expect "bad query" "$(status --get --data-urlencode 'q=level:' "$base_url/api/search")" "422"

# Milestone 3: the query language.
expect "OR" "$(query 'first OR third')" "third first"
expect "NOT" "$(query 'service:smoke NOT first')" "second"
expect "field exists" "$(query 'service:*')" "second first"
expect "phrase, NOT around it (checked, not indexed)" "$(query 'NOT "smoke second"')" "third second first"
detail="$(curl --silent --get --data-urlencode 'q=first (second' "$base_url/api/search")"
expect "error position" "$(python3 -c 'import json, sys; print(json.loads(sys.argv[1])["detail"]["position"])' "$detail")" "6"
grouped="$(curl --silent --get --data-urlencode 'q=first OR second third' "$base_url/api/search")"
expect "grouping shown" "$(field "$grouped" query)" "first OR (second third)"

echo "smoke test passed"
