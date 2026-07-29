#!/usr/bin/env bash
# Run the agent container locally and hit /ping + /invocations, exactly the two
# endpoints AgentCore Runtime calls. Fastest way to catch problems before a deploy.
set -euo pipefail
cd "$(dirname "$0")/.."

REGION="${REGION:-us-east-2}"
PORT="${PORT:-18080}"
NAME=nova2-local

if [[ -z "${GATEWAY_URL:-}" && -f ../11-vpc-no-egress-workaround/state.env ]]; then
  GATEWAY_URL=$(grep '^GW_URL=' ../11-vpc-no-egress-workaround/state.env | cut -d= -f2-)
fi
: "${GATEWAY_URL:?set GATEWAY_URL}"

echo "==> Building image"
docker build --platform linux/arm64 -f docker/Dockerfile -t strands-nova2-orderdesk:latest . >/dev/null

# The container needs real credentials; export whatever this shell is using.
eval "$(aws configure export-credentials --format env)"

docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" -p "$PORT:8080" \
  -e AWS_REGION="$REGION" -e GATEWAY_URL="$GATEWAY_URL" \
  -e MODEL_ID="${MODEL_ID:-global.amazon.nova-2-lite-v1:0}" \
  -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_SESSION_TOKEN \
  strands-nova2-orderdesk:latest >/dev/null
trap 'docker rm -f "$NAME" >/dev/null 2>&1 || true' EXIT

echo "==> Waiting for /ping"
until curl -sf "http://localhost:$PORT/ping" >/dev/null 2>&1; do sleep 2; done
curl -s "http://localhost:$PORT/ping"; echo

echo "==> POST /invocations"
curl -s -X POST "http://localhost:$PORT/invocations" -H 'Content-Type: application/json' \
  -d "{\"prompt\":\"${1:-How many orders are pending?}\"}" | python3 -m json.tool

echo "==> Container logs (tail)"
docker logs "$NAME" 2>&1 | tail -12
