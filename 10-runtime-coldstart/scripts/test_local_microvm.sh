#!/usr/bin/env bash
# Local contract test for the Lambda MicroVMs ping-pong image (microvm/):
#   GET  :8080/ping                                   -> {"status": "Healthy"}
#   POST :8080/invocations                            -> pong + run_hook_ts
#   POST :9000/aws/lambda-microvms/runtime/v1/<hook>  -> 200 for all six hooks
# Builds the UNPADDED image (the pad layers are only for size calibration and
# are exercised by the real Lambda build in scripts/deploy_microvm.sh).
set -euo pipefail
cd "$(dirname "$0")/.."

IMAGE=coldstart-pingpong-microvm
PORT=18080
HOOK_PORT=19000
CID=""

cleanup() {
    [ -n "$CID" ] && docker rm -f "$CID" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "== Building $IMAGE:local (linux/arm64, no pad) =="
docker build --platform linux/arm64 -f microvm/Dockerfile -t "$IMAGE:local" microvm/ >/dev/null
echo "size: $(( $(docker image inspect --format '{{.Size}}' "$IMAGE:local") / 1024 / 1024 )) MB"

CID=$(docker run -d -p "${PORT}:8080" -p "${HOOK_PORT}:9000" "$IMAGE:local")

ping_out=""
for _ in $(seq 1 30); do
    if ping_out=$(curl -sf "http://localhost:${PORT}/ping" 2>/dev/null); then
        break
    fi
    sleep 1
done
if [ -z "$ping_out" ]; then
    echo "FAIL: /ping never became healthy"
    docker logs "$CID" | tail -10
    exit 1
fi
echo "/ping -> $ping_out"
echo "$ping_out" | grep -q '"Healthy"' || { echo "FAIL: /ping body wrong"; exit 1; }

HOOKS=/aws/lambda-microvms/runtime/v1
for hook in ready validate run resume suspend terminate; do
    body='{}'
    [ "$hook" = "run" ] && body='{"microvmId":"microvm-local-test","runHookPayload":"probe"}'
    code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "http://localhost:${HOOK_PORT}${HOOKS}/${hook}" \
        -H 'Content-Type: application/json' -d "$body")
    echo "hook /$hook -> $code"
    [ "$code" = "200" ] || { echo "FAIL: hook $hook returned $code"; exit 1; }
done
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "http://localhost:${HOOK_PORT}${HOOKS}/bogus")
[ "$code" = "404" ] || { echo "FAIL: unknown hook should 404, got $code"; exit 1; }

inv_out=$(curl -sf -X POST "http://localhost:${PORT}/invocations" \
    -H 'Content-Type: application/json' -d '{"ping":"test"}')
echo "/invocations -> $inv_out"
echo "$inv_out" | grep -q '"pong"'               || { echo "FAIL: no pong"; exit 1; }
echo "$inv_out" | grep -q '"lambda-microvm"'     || { echo "FAIL: no origin"; exit 1; }
echo "$inv_out" | grep -q '"proc_start_ts"'      || { echo "FAIL: no proc_start_ts"; exit 1; }
echo "$inv_out" | grep -q '"microvm-local-test"' || { echo "FAIL: /run payload not recorded"; exit 1; }
echo "$inv_out" | grep -q '"run_hook_ts": [0-9]' || { echo "FAIL: run_hook_ts not set by /run"; exit 1; }

echo "PASS — MicroVM image satisfies the agent + lifecycle-hook contract."
