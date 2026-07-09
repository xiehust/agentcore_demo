#!/usr/bin/env bash
# Verify each built image satisfies the AgentCore HTTP contract locally:
#   GET  /ping        -> {"status": "Healthy"}
#   POST /invocations -> {"message": "pong", "proc_start_ts": ...}
set -euo pipefail

IMAGE=coldstart-pingpong
PORT=18080
CID=""

cleanup() {
    [ -n "$CID" ] && docker rm -f "$CID" >/dev/null 2>&1 || true
}
trap cleanup EXIT

for tag in 500mb 1gb 2gb; do
    echo "== Testing $IMAGE:$tag =="
    CID=$(docker run -d -p "${PORT}:8080" "$IMAGE:$tag")

    ping_out=""
    for _ in $(seq 1 30); do
        if ping_out=$(curl -sf "http://localhost:${PORT}/ping" 2>/dev/null); then
            break
        fi
        sleep 1
    done
    if [ -z "$ping_out" ]; then
        echo "FAIL $tag: /ping never became healthy"
        docker logs "$CID" | tail -5
        exit 1
    fi
    echo "$tag /ping -> $ping_out"
    echo "$ping_out" | grep -q '"Healthy"' || { echo "FAIL $tag: /ping body wrong"; exit 1; }

    inv_out=$(curl -sf -X POST "http://localhost:${PORT}/invocations" \
        -H 'Content-Type: application/json' -d '{"ping":"test"}')
    echo "$tag /invocations -> $inv_out"
    echo "$inv_out" | grep -q '"pong"'          || { echo "FAIL $tag: no pong"; exit 1; }
    echo "$inv_out" | grep -q '"proc_start_ts"' || { echo "FAIL $tag: no proc_start_ts"; exit 1; }

    docker rm -f "$CID" >/dev/null
    CID=""
    echo "PASS $tag"
done
echo "All local contract tests passed."
