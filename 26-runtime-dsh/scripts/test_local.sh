#!/usr/bin/env bash
# Local contract test: run the image with the port published on loopback only,
# then check /ping, a ping invocation and (with AWS credentials) one agent turn.
set -euo pipefail
cd "$(dirname "$0")/.."

DSH_VERSION="${DSH_VERSION:-0.2.2-rc.1}"
IMAGE="${IMAGE:-dsh-coldstart}:${TAG:-dsh-${DSH_VERSION}}"
REGION="${REGION:-us-east-1}"
PORT="${LOCAL_PORT:-18080}"
NAME=dsh-coldstart-local

# Credentials reach the container as environment variables and are never printed.
eval "$(aws configure export-credentials --format env)"
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" --platform linux/arm64 -p "127.0.0.1:${PORT}:8080" \
    -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_SESSION_TOKEN \
    -e "AWS_REGION=${REGION}" -e "DSH_PREWARM=${DSH_PREWARM:-0}" "$IMAGE" >/dev/null
trap 'docker rm -f "$NAME" >/dev/null 2>&1 || true' EXIT

for _ in $(seq 1 30); do
    curl -sf "127.0.0.1:${PORT}/ping" >/dev/null 2>&1 && break
    sleep 0.5
done
echo "ping:   $(curl -sf "127.0.0.1:${PORT}/ping")"
echo "invoke: $(curl -sf -XPOST "127.0.0.1:${PORT}/invocations" -d '{"mode":"ping"}')"
for turn in 1 2; do
    curl -sf -XPOST "127.0.0.1:${PORT}/invocations" \
        -d '{"mode":"agent","prompt":"Reply with exactly: pong"}' | python3 -c "
import json, sys
d = json.load(sys.stdin); a = d['agent']
ms = lambda x: None if a[x] is None else round((a[x] - a['spawn_ts']) * 1000)
print('agent turn $turn: ok=%s output=%r dsh_boot_ms=%s first_text_ms=%s dsh_total_ms=%s cred=%s error=%s' % (
    a['ok'], a['output'], ms('first_output_ts'), ms('first_text_ts'), ms('exit_ts'), a['cred_source'], a['error']))
sys.exit(0 if a['ok'] else 1)"
done
echo "user:   $(docker exec "$NAME" id -un)"
echo "PASS"
