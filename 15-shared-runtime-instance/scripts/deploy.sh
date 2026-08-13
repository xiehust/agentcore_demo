#!/usr/bin/env bash
# Build the arm64 image, push to ECR, create (or update) the AgentCore
# runtime bound to an ARM64 capacity provider, wait for READY, and write
# the selected runtime config for the test client.
set -euo pipefail

REGION="${REGION:-us-west-2}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
REPO="${REPO:-launchpad-agents}"
TAG="${TAG:-shared-runtime-v1}"
IMAGE_URI="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${REPO}:${TAG}"

RUNTIME_NAME="${RUNTIME_NAME:-shared_runtime_multiuser}"
ROLE_ARN="${ROLE_ARN:-arn:aws:iam::${ACCOUNT_ID}:role/AmazonBedrockAgentCoreSDKRuntime-us-west-2-6b8cf5ef59}"
CAPACITY_PROVIDER_ARN="${CAPACITY_PROVIDER_ARN:-arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT_ID}:capacity-provider/capacity_provider_arm_kb-FQtDNVGq1t}"
MODEL_ID="${MODEL_ID:-us.anthropic.claude-sonnet-4-6}"
MAX_PARALLEL_AGENTS="${MAX_PARALLEL_AGENTS:-16}"
MAX_TURNS="${MAX_TURNS:-64}"
SKIP_IMAGE_BUILD="${SKIP_IMAGE_BUILD:-0}"
RUNTIME_CONFIG="${RUNTIME_CONFIG:-runtime.json}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

if [[ "${SKIP_IMAGE_BUILD}" == "1" ]]; then
  echo "== [1/4] reuse existing image ${IMAGE_URI} =="
  aws ecr describe-images \
    --repository-name "${REPO}" \
    --image-ids imageTag="${TAG}" \
    --region "${REGION}" >/dev/null
  echo "== [2/4] image push skipped =="
else
  echo "== [1/4] docker build (linux/arm64) =="
  docker build --platform linux/arm64 -f docker/Dockerfile -t "${IMAGE_URI}" .

  echo "== [2/4] push to ECR =="
  aws ecr get-login-password --region "${REGION}" \
    | docker login --username AWS --password-stdin \
        "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
  docker push "${IMAGE_URI}"
fi

echo "== [3/4] create or update agent runtime =="
EXISTING_ID="$(aws bedrock-agentcore-control list-agent-runtimes \
  --region "${REGION}" \
  --query "agentRuntimes[?agentRuntimeName=='${RUNTIME_NAME}'].agentRuntimeId | [0]" \
  --output text | grep -v '^None$' | head -1 || true)"

if [[ -z "${EXISTING_ID}" ]]; then
  RUNTIME_ID="$(aws bedrock-agentcore-control create-agent-runtime \
    --agent-runtime-name "${RUNTIME_NAME}" \
    --role-arn "${ROLE_ARN}" \
    --agent-runtime-artifact "{\"containerConfiguration\":{\"containerUri\":\"${IMAGE_URI}\"}}" \
    --capacity-provider-configuration "{\"capacityProviderArn\":\"${CAPACITY_PROVIDER_ARN}\"}" \
    --filesystem-configurations '[{"capacityProviderVolume":{"volumeName":"scratch","mountPath":"/mnt/scratch"}}]' \
    --environment-variables "{\"ANTHROPIC_MODEL\":\"${MODEL_ID}\",\"AWS_REGION\":\"${REGION}\",\"AWS_DEFAULT_REGION\":\"${REGION}\",\"MAX_PARALLEL_AGENTS\":\"${MAX_PARALLEL_AGENTS}\",\"MAX_TURNS\":\"${MAX_TURNS}\"}" \
    --region "${REGION}" \
    --query agentRuntimeId --output text)"
  echo "created runtime: ${RUNTIME_ID}"
else
  RUNTIME_ID="${EXISTING_ID}"
  aws bedrock-agentcore-control update-agent-runtime \
    --agent-runtime-id "${RUNTIME_ID}" \
    --role-arn "${ROLE_ARN}" \
    --agent-runtime-artifact "{\"containerConfiguration\":{\"containerUri\":\"${IMAGE_URI}\"}}" \
    --capacity-provider-configuration "{\"capacityProviderArn\":\"${CAPACITY_PROVIDER_ARN}\"}" \
    --filesystem-configurations '[{"capacityProviderVolume":{"volumeName":"scratch","mountPath":"/mnt/scratch"}}]' \
    --environment-variables "{\"ANTHROPIC_MODEL\":\"${MODEL_ID}\",\"AWS_REGION\":\"${REGION}\",\"AWS_DEFAULT_REGION\":\"${REGION}\",\"MAX_PARALLEL_AGENTS\":\"${MAX_PARALLEL_AGENTS}\",\"MAX_TURNS\":\"${MAX_TURNS}\"}" \
    --region "${REGION}" >/dev/null
  echo "updated runtime: ${RUNTIME_ID}"
fi

echo "== [4/4] wait for READY =="
for _ in $(seq 1 60); do
  STATUS="$(aws bedrock-agentcore-control get-agent-runtime \
    --agent-runtime-id "${RUNTIME_ID}" --region "${REGION}" \
    --query status --output text)"
  echo "  status=${STATUS}"
  [[ "${STATUS}" == "READY" ]] && break
  [[ "${STATUS}" == *FAILED* ]] && { echo "runtime failed" >&2; exit 1; }
  sleep 5
done
[[ "${STATUS}" == "READY" ]] || { echo "timeout waiting for READY" >&2; exit 1; }

RUNTIME_ARN="$(aws bedrock-agentcore-control get-agent-runtime \
  --agent-runtime-id "${RUNTIME_ID}" --region "${REGION}" \
  --query agentRuntimeArn --output text)"

cat > "${RUNTIME_CONFIG}" <<EOF
{
  "region": "${REGION}",
  "runtimeName": "${RUNTIME_NAME}",
  "runtimeId": "${RUNTIME_ID}",
  "runtimeArn": "${RUNTIME_ARN}",
  "imageUri": "${IMAGE_URI}",
  "modelId": "${MODEL_ID}",
  "capacityProviderArn": "${CAPACITY_PROVIDER_ARN}"
}
EOF
echo "wrote ${RUNTIME_CONFIG} (${RUNTIME_ARN})"
