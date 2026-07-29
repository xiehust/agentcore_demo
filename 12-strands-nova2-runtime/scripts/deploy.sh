#!/usr/bin/env bash
# Build the arm64 image, push to ECR, create/refresh the IAM execution role, and
# create or update a PUBLIC AgentCore Runtime. Idempotent.
set -euo pipefail
cd "$(dirname "$0")/.."
export AWS_PAGER=""

REGION="${REGION:-us-east-2}"
REPO="${REPO:-strands-nova2-orderdesk}"
TAG="${TAG:-latest}"
RUNTIME_NAME="${RUNTIME_NAME:-strands_nova2_orderdesk}"
ROLE_NAME="${ROLE_NAME:-StrandsNova2RuntimeRole}"
MODEL_ID="${MODEL_ID:-global.amazon.nova-2-lite-v1:0}"
IDLE_TIMEOUT=900

# The gateway built in 11-vpc-no-egress-workaround is what gives this agent
# access to the private RDS. Override GATEWAY_URL to point elsewhere.
if [[ -z "${GATEWAY_URL:-}" && -f ../11-vpc-no-egress-workaround/state.env ]]; then
  GATEWAY_URL=$(grep '^GW_URL=' ../11-vpc-no-egress-workaround/state.env | cut -d= -f2-)
  GATEWAY_ARN=$(grep '^GW_ARN=' ../11-vpc-no-egress-workaround/state.env | cut -d= -f2-)
fi
: "${GATEWAY_URL:?set GATEWAY_URL (or run 11-vpc-no-egress-workaround first)}"
GATEWAY_ARN="${GATEWAY_ARN:-*}"

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
ECR_URI="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com/${REPO}"
echo "account=$ACCOUNT region=$REGION model=$MODEL_ID"
echo "gateway=$GATEWAY_URL"

# ------------------------------------------------------------------ 1. build
echo "==> Building linux/arm64 image"
docker build --platform linux/arm64 -f docker/Dockerfile -t "$REPO:$TAG" . >/dev/null
arch=$(docker image inspect "$REPO:$TAG" --format '{{.Architecture}}')
[[ "$arch" == "arm64" ]] || { echo "FATAL: image arch is $arch, AgentCore needs arm64" >&2; exit 1; }
echo "  ok arm64 image built"

# -------------------------------------------------------------------- 2. ECR
aws ecr describe-repositories --repository-names "$REPO" --region "$REGION" >/dev/null 2>&1 \
  || aws ecr create-repository --repository-name "$REPO" --region "$REGION" >/dev/null
aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com" >/dev/null
docker tag "$REPO:$TAG" "$ECR_URI:$TAG"
echo "==> Pushing $ECR_URI:$TAG"
docker push "$ECR_URI:$TAG" | tail -1

# -------------------------------------------------------------------- 3. IAM
REPO_ARN="arn:aws:ecr:${REGION}:${ACCOUNT}:repository/${REPO}"
if ! aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  echo "==> Creating execution role $ROLE_NAME"
  aws iam create-role --role-name "$ROLE_NAME" --assume-role-policy-document "{
    \"Version\":\"2012-10-17\",
    \"Statement\":[{
      \"Effect\":\"Allow\",
      \"Principal\":{\"Service\":\"bedrock-agentcore.amazonaws.com\"},
      \"Action\":\"sts:AssumeRole\",
      \"Condition\":{\"StringEquals\":{\"aws:SourceAccount\":\"$ACCOUNT\"}}
    }]}" >/dev/null
fi

# Nova 2 Lite is invoked through the *global* inference profile, which can route
# to any region -- so both the profile and the underlying foundation model are
# granted with a wildcard region.
aws iam put-role-policy --role-name "$ROLE_NAME" --policy-name StrandsNova2RuntimePolicy \
  --policy-document "{
  \"Version\":\"2012-10-17\",
  \"Statement\":[
    {\"Sid\":\"ECRAuth\",\"Effect\":\"Allow\",\"Action\":\"ecr:GetAuthorizationToken\",\"Resource\":\"*\"},
    {\"Sid\":\"ECRPull\",\"Effect\":\"Allow\",
     \"Action\":[\"ecr:BatchGetImage\",\"ecr:GetDownloadUrlForLayer\",\"ecr:BatchCheckLayerAvailability\"],
     \"Resource\":\"$REPO_ARN\"},
    {\"Sid\":\"Logs\",\"Effect\":\"Allow\",
     \"Action\":[\"logs:CreateLogGroup\",\"logs:CreateLogStream\",\"logs:PutLogEvents\",
                 \"logs:DescribeLogStreams\",\"logs:DescribeLogGroups\"],
     \"Resource\":\"arn:aws:logs:${REGION}:${ACCOUNT}:*\"},
    {\"Sid\":\"XRay\",\"Effect\":\"Allow\",
     \"Action\":[\"xray:PutTraceSegments\",\"xray:PutTelemetryRecords\"],\"Resource\":\"*\"},
    {\"Sid\":\"CWMetrics\",\"Effect\":\"Allow\",\"Action\":\"cloudwatch:PutMetricData\",\"Resource\":\"*\"},
    {\"Sid\":\"InvokeNova2\",\"Effect\":\"Allow\",
     \"Action\":[\"bedrock:InvokeModel\",\"bedrock:InvokeModelWithResponseStream\"],
     \"Resource\":[
       \"arn:aws:bedrock:*:${ACCOUNT}:inference-profile/${MODEL_ID}\",
       \"arn:aws:bedrock:*::foundation-model/amazon.nova-2-lite-v1:0\"]},
    {\"Sid\":\"InvokeGateway\",\"Effect\":\"Allow\",
     \"Action\":\"bedrock-agentcore:InvokeGateway\",\"Resource\":\"$GATEWAY_ARN\"}
  ]}" >/dev/null
ROLE_ARN="arn:aws:iam::${ACCOUNT}:role/${ROLE_NAME}"
echo "  ok role $ROLE_ARN"

# ---------------------------------------------------------------- 4. runtime
runtime_id=$(aws bedrock-agentcore-control list-agent-runtimes --region "$REGION" \
  --query "agentRuntimes[?agentRuntimeName=='$RUNTIME_NAME'].agentRuntimeId" --output text \
  | awk 'NF && $1 != "None" {print $1; exit}')

CONFIG="{
  \"agentRuntimeArtifact\":{\"containerConfiguration\":{\"containerUri\":\"$ECR_URI:$TAG\"}},
  \"networkConfiguration\":{\"networkMode\":\"PUBLIC\"},
  \"protocolConfiguration\":{\"serverProtocol\":\"HTTP\"},
  \"roleArn\":\"$ROLE_ARN\",
  \"lifecycleConfiguration\":{\"idleRuntimeSessionTimeout\":$IDLE_TIMEOUT},
  \"environmentVariables\":{\"MODEL_ID\":\"$MODEL_ID\",\"GATEWAY_URL\":\"$GATEWAY_URL\"}
}"

if [[ -n "$runtime_id" ]]; then
  echo "==> Updating runtime $RUNTIME_NAME ($runtime_id)"
  aws bedrock-agentcore-control update-agent-runtime --region "$REGION" \
    --agent-runtime-id "$runtime_id" --cli-input-json "$CONFIG" >/dev/null
else
  echo "==> Creating PUBLIC runtime $RUNTIME_NAME"
  for attempt in 1 2 3 4 5 6; do
    if out=$(aws bedrock-agentcore-control create-agent-runtime --region "$REGION" \
          --cli-input-json "{\"agentRuntimeName\":\"$RUNTIME_NAME\",${CONFIG#\{}" 2>&1); then
      runtime_id=$(echo "$out" | python3 -c 'import json,sys; print(json.load(sys.stdin)["agentRuntimeId"])')
      break
    fi
    # "Role validation failed" on a freshly created role is IAM propagation, not a
    # real config error -- keep retrying. Any other ValidationException is fatal.
    if echo "$out" | grep -q ValidationException && ! echo "$out" | grep -qi 'Role validation failed'; then
      echo "FATAL: $out" >&2; exit 1
    fi
    echo "  attempt $attempt failed (likely IAM propagation), retrying: $(echo "$out" | tail -1)"
    sleep 15
  done
fi
[[ -n "$runtime_id" ]] || { echo "FATAL: runtime not created" >&2; exit 1; }

echo "==> Waiting for READY"
for _ in $(seq 60); do
  read -r status <<<"$(aws bedrock-agentcore-control get-agent-runtime --region "$REGION" \
    --agent-runtime-id "$runtime_id" --query 'status' --output text)"
  [[ "$status" == "READY" ]] && break
  [[ "$status" == *FAILED* ]] && { aws bedrock-agentcore-control get-agent-runtime \
      --region "$REGION" --agent-runtime-id "$runtime_id"; exit 1; }
  sleep 10
done
ARN=$(aws bedrock-agentcore-control get-agent-runtime --region "$REGION" \
  --agent-runtime-id "$runtime_id" --query 'agentRuntimeArn' --output text)

cat > runtime.json <<JSON
{
  "region": "$REGION",
  "runtimeName": "$RUNTIME_NAME",
  "runtimeId": "$runtime_id",
  "runtimeArn": "$ARN",
  "roleArn": "$ROLE_ARN",
  "imageUri": "$ECR_URI:$TAG",
  "modelId": "$MODEL_ID",
  "gatewayUrl": "$GATEWAY_URL",
  "networkMode": "PUBLIC",
  "status": "$status"
}
JSON
echo "  ok status=$status"
echo "  arn=$ARN"
echo "Wrote runtime.json"
