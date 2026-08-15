#!/usr/bin/env bash
# Build/push the image and create or update a default AgentCore microVM Runtime.
# This script intentionally supplies no capacityProviderConfiguration or
# filesystemConfigurations.
set -euo pipefail
export AWS_PAGER=""

REGION="${REGION:-us-west-2}"
RUNTIME_NAME="${RUNTIME_NAME:-shared_runtime_microvm}"
REPO="${REPO:-launchpad-agents}"
TAG="${TAG:-shared-runtime-microvm-v1}"
MODEL_ID="${MODEL_ID:-us.anthropic.claude-sonnet-4-6}"
MAX_PARALLEL_AGENTS="${MAX_PARALLEL_AGENTS:-8}"
MAX_TURNS="${MAX_TURNS:-64}"
SKIP_IMAGE_BUILD="${SKIP_IMAGE_BUILD:-0}"
RUNTIME_CONFIG="${RUNTIME_CONFIG:-runtime.json}"
: "${ROLE_ARN:?Set ROLE_ARN to the AgentCore Runtime execution role ARN}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
IMAGE_URI="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${REPO}:${TAG}"

for tool in aws docker python3; do
  command -v "${tool}" >/dev/null || { echo "missing required tool: ${tool}" >&2; exit 1; }
done

if [[ "${SKIP_IMAGE_BUILD}" == "1" ]]; then
  echo "== [1/4] verify existing image ${IMAGE_URI} =="
  aws ecr describe-images --repository-name "${REPO}" \
    --image-ids imageTag="${TAG}" --region "${REGION}" >/dev/null
else
  echo "== [1/4] build and push linux/arm64 image =="
  aws ecr describe-repositories --repository-names "${REPO}" \
    --region "${REGION}" >/dev/null 2>&1 \
    || aws ecr create-repository --repository-name "${REPO}" \
         --image-scanning-configuration scanOnPush=true \
         --region "${REGION}" >/dev/null
  aws ecr get-login-password --region "${REGION}" \
    | docker login --username AWS --password-stdin \
        "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
  docker build --platform linux/arm64 -f docker/Dockerfile -t "${IMAGE_URI}" .
  docker push "${IMAGE_URI}"
fi

ENVIRONMENT_JSON="$(python3 - "${MODEL_ID}" "${REGION}" \
  "${MAX_PARALLEL_AGENTS}" "${MAX_TURNS}" <<'PY'
import json
import sys
model, region, parallel, turns = sys.argv[1:]
print(json.dumps({
    "ANTHROPIC_MODEL": model,
    "AWS_REGION": region,
    "AWS_DEFAULT_REGION": region,
    "MAX_PARALLEL_AGENTS": parallel,
    "MAX_TURNS": turns,
    "USERS_ROOT": "/tmp/agentcore-users",
}, separators=(",", ":")))
PY
)"
ARTIFACT_JSON="$(python3 - "${IMAGE_URI}" <<'PY'
import json
import sys
print(json.dumps({"containerConfiguration": {"containerUri": sys.argv[1]}}))
PY
)"

echo "== [2/4] create or update default microVM Runtime =="
EXISTING_ID="$(aws bedrock-agentcore-control list-agent-runtimes \
  --region "${REGION}" \
  --query "agentRuntimes[?agentRuntimeName=='${RUNTIME_NAME}'].agentRuntimeId | [0]" \
  --output text | grep -v '^None$' | head -1 || true)"

if [[ -z "${EXISTING_ID}" ]]; then
  RUNTIME_ID="$(aws bedrock-agentcore-control create-agent-runtime \
    --agent-runtime-name "${RUNTIME_NAME}" \
    --role-arn "${ROLE_ARN}" \
    --agent-runtime-artifact "${ARTIFACT_JSON}" \
    --network-configuration '{"networkMode":"PUBLIC"}' \
    --environment-variables "${ENVIRONMENT_JSON}" \
    --region "${REGION}" --query agentRuntimeId --output text)"
  echo "created Runtime ${RUNTIME_ID}"
else
  RUNTIME_ID="${EXISTING_ID}"
  PROVIDER="$(aws bedrock-agentcore-control get-agent-runtime \
    --agent-runtime-id "${RUNTIME_ID}" --region "${REGION}" \
    --query 'capacityProviderConfiguration.capacityProviderArn' --output text)"
  FILESYSTEMS="$(aws bedrock-agentcore-control get-agent-runtime \
    --agent-runtime-id "${RUNTIME_ID}" --region "${REGION}" \
    --query 'filesystemConfigurations' --output json)"
  if [[ -n "${PROVIDER}" && "${PROVIDER}" != "None" ]]; then
    echo "refusing to convert capacity-provider Runtime ${RUNTIME_ID}; choose another RUNTIME_NAME" >&2
    exit 1
  fi
  if [[ "${FILESYSTEMS}" != "null" && "${FILESYSTEMS}" != "[]" ]]; then
    echo "refusing to retain filesystem configuration on Runtime ${RUNTIME_ID}; choose another RUNTIME_NAME" >&2
    exit 1
  fi
  aws bedrock-agentcore-control update-agent-runtime \
    --agent-runtime-id "${RUNTIME_ID}" \
    --role-arn "${ROLE_ARN}" \
    --agent-runtime-artifact "${ARTIFACT_JSON}" \
    --network-configuration '{"networkMode":"PUBLIC"}' \
    --environment-variables "${ENVIRONMENT_JSON}" \
    --region "${REGION}" >/dev/null
  echo "updated Runtime ${RUNTIME_ID}"
fi

echo "== [3/4] wait for READY =="
STATUS=""
for _ in $(seq 1 120); do
  STATUS="$(aws bedrock-agentcore-control get-agent-runtime \
    --agent-runtime-id "${RUNTIME_ID}" --region "${REGION}" \
    --query status --output text)"
  echo "  status=${STATUS}"
  [[ "${STATUS}" == "READY" ]] && break
  [[ "${STATUS}" == *FAILED* ]] && { echo "Runtime deployment failed" >&2; exit 1; }
  sleep 5
done
[[ "${STATUS}" == "READY" ]] || { echo "timed out waiting for READY" >&2; exit 1; }

RUNTIME_ARN="$(aws bedrock-agentcore-control get-agent-runtime \
  --agent-runtime-id "${RUNTIME_ID}" --region "${REGION}" \
  --query agentRuntimeArn --output text)"
TEMP_CONFIG="${RUNTIME_CONFIG}.tmp"
python3 - "${TEMP_CONFIG}" "${REGION}" "${RUNTIME_NAME}" "${RUNTIME_ID}" \
  "${RUNTIME_ARN}" "${IMAGE_URI}" "${MODEL_ID}" <<'PY'
import json
import sys
path, region, name, runtime_id, arn, image, model = sys.argv[1:]
with open(path, "w", encoding="utf-8") as handle:
    json.dump({
        "region": region,
        "runtimeName": name,
        "runtimeId": runtime_id,
        "runtimeArn": arn,
        "imageUri": image,
        "modelId": model,
        "computeType": "microvm",
    }, handle, indent=2)
    handle.write("\n")
PY
mv "${TEMP_CONFIG}" "${RUNTIME_CONFIG}"
echo "== [4/4] wrote ${RUNTIME_CONFIG} (${RUNTIME_ARN}) =="
