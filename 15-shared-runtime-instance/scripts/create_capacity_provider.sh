#!/usr/bin/env bash
# Clone the network, IAM, storage, and lifecycle settings from an existing
# AgentCore Capacity Provider while changing the allowed EC2 instance type.
set -euo pipefail

REGION="${REGION:-us-west-2}"
SOURCE_CAPACITY_PROVIDER_ID="${SOURCE_CAPACITY_PROVIDER_ID:-capacity_provider_arm_kb-FQtDNVGq1t}"
CAPACITY_PROVIDER_NAME="${CAPACITY_PROVIDER_NAME:-capacity_provider_arm_m7g_large}"
INSTANCE_TYPE="${INSTANCE_TYPE:-m7g.large}"
DESCRIPTION="${DESCRIPTION:-ARM64 ${INSTANCE_TYPE} capacity provider for shared runtime load tests}"

existing_id="$(
  aws bedrock-agentcore-control list-capacity-providers \
    --region "${REGION}" \
    --query "capacityProviders[?name=='${CAPACITY_PROVIDER_NAME}'].capacityProviderId | [0]" \
    --output text | grep -v '^None$' | head -1 || true
)"

if [[ -n "${existing_id}" ]]; then
  status="$(
    aws bedrock-agentcore-control get-capacity-provider \
      --capacity-provider-id "${existing_id}" \
      --region "${REGION}" \
      --query status --output text
  )"
  [[ "${status}" == "READY" ]] || {
    echo "existing capacity provider ${existing_id} is ${status}" >&2
    exit 1
  }
  aws bedrock-agentcore-control get-capacity-provider \
    --capacity-provider-id "${existing_id}" \
    --region "${REGION}" \
    --query capacityProviderArn --output text
  exit 0
fi

source_json="$(
  aws bedrock-agentcore-control get-capacity-provider \
    --capacity-provider-id "${SOURCE_CAPACITY_PROVIDER_ID}" \
    --region "${REGION}" \
    --output json
)"

payload="$(
  jq -n \
    --arg name "${CAPACITY_PROVIDER_NAME}" \
    --arg description "${DESCRIPTION}" \
    --arg instance_type "${INSTANCE_TYPE}" \
    --argjson permissions "$(jq '.permissionsConfiguration' <<<"${source_json}")" \
    --argjson compute "$(jq '.computeConfiguration' <<<"${source_json}")" \
    '{
      name: $name,
      description: $description,
      permissionsConfiguration: $permissions,
      computeConfiguration:
        ($compute
          | .ec2Configuration.launchTemplateSource.launchParameters
              .instanceRequirements.allowedInstanceTypes = [$instance_type])
    }'
)"

capacity_provider_id="$(
  aws bedrock-agentcore-control create-capacity-provider \
    --cli-input-json "${payload}" \
    --region "${REGION}" \
    --query capacityProviderId --output text
)"
echo "created capacity provider: ${capacity_provider_id}" >&2

for _ in $(seq 1 120); do
  status="$(
    aws bedrock-agentcore-control get-capacity-provider \
      --capacity-provider-id "${capacity_provider_id}" \
      --region "${REGION}" \
      --query status --output text
  )"
  echo "  status=${status}" >&2
  [[ "${status}" == "READY" ]] && break
  [[ "${status}" == *FAILED* ]] && {
    aws bedrock-agentcore-control get-capacity-provider \
      --capacity-provider-id "${capacity_provider_id}" \
      --region "${REGION}" --output json >&2
    exit 1
  }
  sleep 5
done

[[ "${status}" == "READY" ]] || {
  echo "timeout waiting for capacity provider READY" >&2
  exit 1
}

aws bedrock-agentcore-control get-capacity-provider \
  --capacity-provider-id "${capacity_provider_id}" \
  --region "${REGION}" \
  --query capacityProviderArn --output text
