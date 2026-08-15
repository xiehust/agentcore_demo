#!/usr/bin/env bash
# Delete the demo Runtime. The execution role, ECR repository, and image remain
# unless DELETE_ECR_IMAGE=1 is explicitly supplied.
set -euo pipefail
export AWS_PAGER=""

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_CONFIG="${RUNTIME_CONFIG:-${ROOT}/runtime.json}"
DELETE_ECR_IMAGE="${DELETE_ECR_IMAGE:-0}"
[[ -f "${RUNTIME_CONFIG}" ]] || { echo "missing ${RUNTIME_CONFIG}" >&2; exit 1; }

mapfile -t VALUES < <(python3 - "${RUNTIME_CONFIG}" <<'PY'
import json
import sys
config = json.load(open(sys.argv[1], encoding="utf-8"))
print(config["region"])
print(config["runtimeId"])
print(config.get("imageUri", ""))
PY
)
REGION="${REGION:-${VALUES[0]}}"
RUNTIME_ID="${VALUES[1]}"
IMAGE_URI="${VALUES[2]}"

echo "deleting AgentCore Runtime ${RUNTIME_ID} ..."
aws bedrock-agentcore-control delete-agent-runtime \
  --agent-runtime-id "${RUNTIME_ID}" --region "${REGION}"

if [[ "${DELETE_ECR_IMAGE}" == "1" && -n "${IMAGE_URI}" ]]; then
  REPOSITORY="${IMAGE_URI#*/}"
  REPOSITORY="${REPOSITORY%:*}"
  TAG="${IMAGE_URI##*:}"
  echo "deleting ECR image ${REPOSITORY}:${TAG} ..."
  aws ecr batch-delete-image --repository-name "${REPOSITORY}" \
    --image-ids imageTag="${TAG}" --region "${REGION}" >/dev/null
else
  echo "ECR image and IAM role retained (set DELETE_ECR_IMAGE=1 to delete the image)."
fi
rm -f "${RUNTIME_CONFIG}"
echo "cleanup request accepted"
