#!/usr/bin/env bash
# Delete the runtime, ECR repo and execution role created by deploy.sh.
# Usage: cleanup.sh --yes
set -euo pipefail
cd "$(dirname "$0")/.."
export AWS_PAGER=""

REGION="${REGION:-us-east-2}"
REPO="${REPO:-strands-nova2-orderdesk}"
RUNTIME_NAME="${RUNTIME_NAME:-strands_nova2_orderdesk}"
ROLE_NAME="${ROLE_NAME:-StrandsNova2RuntimeRole}"

if [[ "${1:-}" != "--yes" ]]; then
  echo "Deletes runtime '$RUNTIME_NAME', ECR repo '$REPO' and role '$ROLE_NAME' in $REGION."
  echo "Re-run with --yes to proceed."
  exit 1
fi
try() { "$@" >/dev/null 2>&1 || true; }

rid=$(aws bedrock-agentcore-control list-agent-runtimes --region "$REGION" \
  --query "agentRuntimes[?agentRuntimeName=='$RUNTIME_NAME'].agentRuntimeId" --output text \
  | awk 'NF && $1 != "None" {print $1; exit}')
if [[ -n "$rid" ]]; then
  echo "Deleting runtime $RUNTIME_NAME ($rid)"
  try aws bedrock-agentcore-control delete-agent-runtime --region "$REGION" --agent-runtime-id "$rid"
fi

echo "Deleting ECR repo $REPO"
try aws ecr delete-repository --repository-name "$REPO" --region "$REGION" --force

echo "Deleting role $ROLE_NAME"
try aws iam delete-role-policy --role-name "$ROLE_NAME" --policy-name StrandsNova2RuntimePolicy
try aws iam delete-role --role-name "$ROLE_NAME"

rm -f runtime.json
echo "Cleanup complete."
echo "NOTE: the gateway / VPC / RDS this agent talks to belong to"
echo "      11-vpc-no-egress-workaround — tear those down separately with its cleanup.sh."
