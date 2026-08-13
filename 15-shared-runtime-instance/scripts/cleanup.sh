#!/usr/bin/env bash
# Delete the demo runtime (keeps the capacity provider and ECR image).
set -euo pipefail

REGION="${REGION:-us-west-2}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

RUNTIME_ID="$(python3 -c "import json;print(json.load(open('${ROOT}/runtime.json'))['runtimeId'])")"

echo "deleting agent runtime ${RUNTIME_ID} ..."
aws bedrock-agentcore-control delete-agent-runtime \
  --agent-runtime-id "${RUNTIME_ID}" --region "${REGION}"
echo "done. capacity provider and ECR image are intentionally kept."
