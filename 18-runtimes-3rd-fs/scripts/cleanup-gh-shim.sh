#!/usr/bin/env bash
# Remove everything 03-deploy-gh-shim-agent.sh created: runtime, API key credential provider
# (and its Secrets Manager secret), IAM role, ECR repository.
# Usage: ./scripts/cleanup-gh-shim.sh   (reads runtime-gh-shim.json)
set -euo pipefail
export AWS_PAGER=""
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CFG="$ROOT/runtime-gh-shim.json"
[[ -f "$CFG" ]] || { echo "no $CFG; nothing to clean" >&2; exit 0; }
REGION=$(jq -r .region "$CFG"); RUNTIME_ID=$(jq -r .runtimeId "$CFG"); PROVIDER=$(jq -r .providerName "$CFG")
ROLE_NAME=$(jq -r .roleArn "$CFG" | awk -F/ '{print $NF}'); REPO=$(jq -r .imageUri "$CFG" | sed -E 's#^[^/]+/##; s#:.*$##')

echo "== delete runtime $RUNTIME_ID"
aws bedrock-agentcore-control delete-agent-runtime --region "$REGION" --agent-runtime-id "$RUNTIME_ID" >/dev/null 2>&1 || true
echo "== delete API key credential provider $PROVIDER (removes the stored GitHub token)"
aws bedrock-agentcore-control delete-api-key-credential-provider --region "$REGION" --name "$PROVIDER" >/dev/null 2>&1 || true
echo "== delete IAM role $ROLE_NAME"
for p in $(aws iam list-role-policies --role-name "$ROLE_NAME" --query 'PolicyNames[]' --output text 2>/dev/null); do
  aws iam delete-role-policy --role-name "$ROLE_NAME" --policy-name "$p"; done
aws iam delete-role --role-name "$ROLE_NAME" 2>/dev/null || true
echo "== delete ECR repo $REPO"
aws ecr delete-repository --region "$REGION" --repository-name "$REPO" --force >/dev/null 2>&1 || true
rm -f "$CFG"
echo "done"
