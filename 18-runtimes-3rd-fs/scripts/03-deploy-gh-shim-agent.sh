#!/usr/bin/env bash
# Deploy the gh-shim verification agent to AgentCore Runtime (microVM, container, PUBLIC network).
#
#   1. store a GitHub token in an AgentCore Identity API key credential provider (Token Vault)
#   2. create the runtime execution role (Bedrock, logs, ECR pull, Identity GetWorkloadAccessToken/GetResourceApiKey)
#   3. build + push the linux/arm64 image (python + gh + git + /opt/shim)
#   4. create the runtime, then set WORKLOAD_NAME from the auto-created workload identity
#   5. write runtime-gh-shim.json for 04-verify-gh-shim.py
#
# Usage:
#   GITHUB_TOKEN_FILE=<file with token>  REGION=us-east-2 ./scripts/03-deploy-gh-shim-agent.sh
#   (or: gh auth token > /tmp/ghtoken && GITHUB_TOKEN_FILE=/tmp/ghtoken ...)
set -euo pipefail
export AWS_PAGER=""

REGION="${REGION:-us-east-2}"
NAME="${NAME:-gh_shim_demo}"
PROVIDER_NAME="${PROVIDER_NAME:-gh-shim-demo-token}"
REPO="${REPO:-agentcore-gh-shim-demo}"
TAG="${TAG:-v1}"
MODEL_ID="${MODEL_ID:-us.anthropic.claude-haiku-4-5-20251001-v1:0}"
: "${GITHUB_TOKEN_FILE:?GITHUB_TOKEN_FILE required (file containing a GitHub token; never pass the token on the command line)}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
IMAGE_URI="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com/${REPO}:${TAG}"
ROLE_NAME="${NAME}-runtime-role"
BUILD="$(mktemp -d)"

echo "== [1/5] API key credential provider ${PROVIDER_NAME} =="
python3 - "$REGION" "$PROVIDER_NAME" "$GITHUB_TOKEN_FILE" <<'PY'
import sys, boto3
region, name, path = sys.argv[1:]
token = open(path, encoding="utf-8").read().strip()
assert token, "token file is empty"
c = boto3.client("bedrock-agentcore-control", region_name=region)
try:
    r = c.create_api_key_credential_provider(name=name, apiKey=token)
    print("  created", r["credentialProviderArn"])
except (c.exceptions.ConflictException, c.exceptions.ValidationException) as exc:
    if "already exists" not in str(exc):
        raise
    r = c.update_api_key_credential_provider(name=name, apiKey=token)
    print("  updated (rotated)", r["credentialProviderArn"])
PY
PROVIDER_ARN="$(aws bedrock-agentcore-control get-api-key-credential-provider --region "$REGION" --name "$PROVIDER_NAME" --query credentialProviderArn --output text)"

echo "== [2/5] execution role ${ROLE_NAME} =="
cat >"$BUILD/trust.json" <<EOF
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"bedrock-agentcore.amazonaws.com"},
 "Action":"sts:AssumeRole","Condition":{"StringEquals":{"aws:SourceAccount":"${ACCOUNT}"}}}]}
EOF
cat >"$BUILD/policy.json" <<EOF
{"Version":"2012-10-17","Statement":[
 {"Sid":"Ecr","Effect":"Allow","Action":["ecr:GetAuthorizationToken"],"Resource":"*"},
 {"Sid":"EcrPull","Effect":"Allow","Action":["ecr:BatchGetImage","ecr:GetDownloadUrlForLayer"],
  "Resource":"arn:aws:ecr:${REGION}:${ACCOUNT}:repository/${REPO}"},
 {"Sid":"Logs","Effect":"Allow","Action":["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents","logs:DescribeLogStreams","logs:DescribeLogGroups"],"Resource":"*"},
 {"Sid":"Telemetry","Effect":"Allow","Action":["xray:PutTraceSegments","xray:PutTelemetryRecords","xray:GetSamplingRules","xray:GetSamplingTargets","cloudwatch:PutMetricData"],"Resource":"*"},
 {"Sid":"Bedrock","Effect":"Allow","Action":["bedrock:InvokeModel","bedrock:InvokeModelWithResponseStream"],"Resource":"*"},
 {"Sid":"WorkloadIdentity","Effect":"Allow","Action":["bedrock-agentcore:GetWorkloadAccessToken","bedrock-agentcore:GetWorkloadAccessTokenForJWT","bedrock-agentcore:GetWorkloadAccessTokenForUserId"],
  "Resource":["arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT}:workload-identity-directory/default","arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT}:workload-identity-directory/default/workload-identity/*"]},
 {"Sid":"TokenVault","Effect":"Allow","Action":["bedrock-agentcore:GetResourceApiKey","bedrock-agentcore:GetResourceOauth2Token"],
  "Resource":["arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT}:token-vault/default","${PROVIDER_ARN}",
              "arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT}:workload-identity-directory/default",
              "arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT}:workload-identity-directory/default/workload-identity/*"]},
 {"Sid":"TokenVaultSecret","Effect":"Allow","Action":["secretsmanager:GetSecretValue"],"Resource":"arn:aws:secretsmanager:${REGION}:${ACCOUNT}:secret:bedrock-agentcore-identity!*"}
]}
EOF
ROLE_ARN="$(aws iam create-role --role-name "$ROLE_NAME" --assume-role-policy-document "file://$BUILD/trust.json" --query Role.Arn --output text 2>/dev/null \
  || aws iam get-role --role-name "$ROLE_NAME" --query Role.Arn --output text)"
aws iam put-role-policy --role-name "$ROLE_NAME" --policy-name "${NAME}-permissions" --policy-document "file://$BUILD/policy.json"
echo "  $ROLE_ARN"

echo "== [3/5] build + push ${IMAGE_URI} =="
aws ecr describe-repositories --repository-names "$REPO" --region "$REGION" >/dev/null 2>&1 \
  || aws ecr create-repository --repository-name "$REPO" --region "$REGION" --image-scanning-configuration scanOnPush=true >/dev/null
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com" >/dev/null
docker build --platform linux/arm64 -f deploy/gh_shim_agent/Dockerfile -t "$IMAGE_URI" .
docker push "$IMAGE_URI" >/dev/null
echo "  pushed"

echo "== [4/5] runtime ${NAME} =="
ENV_JSON="$(python3 -c "import json,sys; print(json.dumps({'AWS_REGION':sys.argv[1],'AWS_DEFAULT_REGION':sys.argv[1],'MODEL_ID':sys.argv[2],'IDENTITY_PROVIDER':sys.argv[3],'TOKEN_SOURCE':'identity','IDENTITY_FLOW':'api_key'}))" "$REGION" "$MODEL_ID" "$PROVIDER_NAME")"
ARTIFACT_JSON="{\"containerConfiguration\":{\"containerUri\":\"${IMAGE_URI}\"}}"
sleep 10  # IAM propagation for a freshly created role
EXISTING_ID="$(aws bedrock-agentcore-control list-agent-runtimes --region "$REGION" \
  --query "agentRuntimes[?agentRuntimeName=='${NAME}'].agentRuntimeId | [0]" --output text | grep -v '^None$' || true)"
if [[ -z "$EXISTING_ID" ]]; then
  RUNTIME_ID="$(aws bedrock-agentcore-control create-agent-runtime --region "$REGION" --agent-runtime-name "$NAME" \
    --role-arn "$ROLE_ARN" --agent-runtime-artifact "$ARTIFACT_JSON" --network-configuration '{"networkMode":"PUBLIC"}' \
    --environment-variables "$ENV_JSON" --query agentRuntimeId --output text)"
  echo "  created $RUNTIME_ID"
else
  RUNTIME_ID="$EXISTING_ID"
fi
WORKLOAD_ARN="$(aws bedrock-agentcore-control get-agent-runtime --region "$REGION" --agent-runtime-id "$RUNTIME_ID" \
  --query 'workloadIdentityDetails.workloadIdentityArn' --output text)"
WORKLOAD_NAME="${WORKLOAD_ARN##*/}"
echo "  workload identity: $WORKLOAD_NAME"
ENV_JSON="$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); d['WORKLOAD_NAME']=sys.argv[2]; print(json.dumps(d))" "$ENV_JSON" "$WORKLOAD_NAME")"
for _ in $(seq 1 60); do
  STATUS="$(aws bedrock-agentcore-control get-agent-runtime --region "$REGION" --agent-runtime-id "$RUNTIME_ID" --query status --output text)"
  [[ "$STATUS" == "READY" ]] && break; [[ "$STATUS" == *FAILED* ]] && { echo "runtime failed" >&2; exit 1; }; sleep 5
done
aws bedrock-agentcore-control update-agent-runtime --region "$REGION" --agent-runtime-id "$RUNTIME_ID" \
  --role-arn "$ROLE_ARN" --agent-runtime-artifact "$ARTIFACT_JSON" --network-configuration '{"networkMode":"PUBLIC"}' \
  --environment-variables "$ENV_JSON" >/dev/null
for _ in $(seq 1 60); do
  STATUS="$(aws bedrock-agentcore-control get-agent-runtime --region "$REGION" --agent-runtime-id "$RUNTIME_ID" --query status --output text)"
  echo "  status=$STATUS"; [[ "$STATUS" == "READY" ]] && break; [[ "$STATUS" == *FAILED* ]] && { echo "runtime failed" >&2; exit 1; }; sleep 5
done
RUNTIME_ARN="$(aws bedrock-agentcore-control get-agent-runtime --region "$REGION" --agent-runtime-id "$RUNTIME_ID" --query agentRuntimeArn --output text)"

echo "== [5/5] runtime-gh-shim.json =="
python3 -c "import json,sys; json.dump(dict(region=sys.argv[1],runtimeName=sys.argv[2],runtimeId=sys.argv[3],runtimeArn=sys.argv[4],roleArn=sys.argv[5],imageUri=sys.argv[6],providerName=sys.argv[7],workloadName=sys.argv[8]), open('runtime-gh-shim.json','w'), indent=2)" \
  "$REGION" "$NAME" "$RUNTIME_ID" "$RUNTIME_ARN" "$ROLE_ARN" "$IMAGE_URI" "$PROVIDER_NAME" "$WORKLOAD_NAME"
cat runtime-gh-shim.json
