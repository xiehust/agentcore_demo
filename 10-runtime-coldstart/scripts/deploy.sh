#!/usr/bin/env bash
# Idempotent deploy: ECR repo + image push + IAM execution role + three
# AgentCore runtimes (coldstart_ping_500mb|1gb|2gb), then poll READY and
# record everything in deployments.json.
set -euo pipefail
cd "$(dirname "$0")/.."

REGION=us-west-2
REPO=agentcore-coldstart-pingpong
IMAGE=coldstart-pingpong
ROLE_NAME=AgentCoreColdstartRole
POLICY_NAME=AgentCoreColdstartPolicy
IDLE_TIMEOUT=60
TAGS=(500mb 1gb 2gb)
MAX_IMAGE_BYTES=$((2048 * 1024 * 1024))   # AgentCore quota: max 2048MB image

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
ECR_URI="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com/${REPO}"
echo "Account: $ACCOUNT  Region: $REGION  Repo: $ECR_URI"

# ---------------------------------------------------------------- 1. ECR repo
if aws ecr describe-repositories --repository-names "$REPO" --region "$REGION" >/dev/null 2>&1; then
    echo "ECR repo exists — skipping create"
else
    echo "Creating ECR repo $REPO"
    aws ecr create-repository --repository-name "$REPO" --region "$REGION" >/dev/null
fi
REPO_ARN="arn:aws:ecr:${REGION}:${ACCOUNT}:repository/${REPO}"

# ------------------------------------------------------------- 2. push images
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com" >/dev/null
declare -A PUSHED
for tag in "${TAGS[@]}"; do
    docker tag "$IMAGE:$tag" "$ECR_URI:$tag"
    remote_digest=$(aws ecr describe-images --repository-name "$REPO" --region "$REGION" \
        --image-ids imageTag="$tag" --query 'imageDetails[0].imageDigest' --output text 2>/dev/null || echo "none")
    if [ "$remote_digest" != "none" ] && docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$ECR_URI:$tag" 2>/dev/null | grep -q "${REPO}@${remote_digest}"; then
        echo "Image $tag already pushed (digest match) — skipping push"
        PUSHED[$tag]=0
    else
        echo "Pushing $ECR_URI:$tag ..."
        docker push "$ECR_URI:$tag" | tail -2
        PUSHED[$tag]=1
    fi
done

# ------------------------------------------------- 3. size guard on 2gb image
for tag in "${TAGS[@]}"; do
    ecr_bytes=$(aws ecr describe-images --repository-name "$REPO" --region "$REGION" \
        --image-ids imageTag="$tag" --query 'imageDetails[0].imageSizeInBytes' --output text)
    echo "ECR $tag imageSizeInBytes=$ecr_bytes ($((ecr_bytes / 1024 / 1024)) MB)"
    if (( ecr_bytes >= MAX_IMAGE_BYTES )); then
        echo "FATAL: ECR image '$tag' is $((ecr_bytes / 1024 / 1024))MB >= 2048MB — AgentCore rejects images over the 2048MB quota. Reduce the pad size in build_images.sh and rebuild." >&2
        exit 1
    fi
done

# ----------------------------------------------------------------- 4. IAM role
if aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
    echo "IAM role exists — skipping create"
else
    echo "Creating IAM role $ROLE_NAME"
    aws iam create-role --role-name "$ROLE_NAME" --assume-role-policy-document "{
      \"Version\": \"2012-10-17\",
      \"Statement\": [{
        \"Effect\": \"Allow\",
        \"Principal\": {\"Service\": \"bedrock-agentcore.amazonaws.com\"},
        \"Action\": \"sts:AssumeRole\",
        \"Condition\": {\"StringEquals\": {\"aws:SourceAccount\": \"$ACCOUNT\"}}
      }]
    }" >/dev/null
fi
aws iam put-role-policy --role-name "$ROLE_NAME" --policy-name "$POLICY_NAME" --policy-document "{
  \"Version\": \"2012-10-17\",
  \"Statement\": [
    {\"Sid\": \"ECRAuth\", \"Effect\": \"Allow\", \"Action\": \"ecr:GetAuthorizationToken\", \"Resource\": \"*\"},
    {\"Sid\": \"ECRPull\", \"Effect\": \"Allow\",
     \"Action\": [\"ecr:BatchGetImage\", \"ecr:GetDownloadUrlForLayer\", \"ecr:BatchCheckLayerAvailability\"],
     \"Resource\": \"$REPO_ARN\"},
    {\"Sid\": \"Logs\", \"Effect\": \"Allow\",
     \"Action\": [\"logs:CreateLogGroup\", \"logs:CreateLogStream\", \"logs:PutLogEvents\", \"logs:DescribeLogStreams\", \"logs:DescribeLogGroups\"],
     \"Resource\": \"arn:aws:logs:${REGION}:${ACCOUNT}:*\"},
    {\"Sid\": \"XRay\", \"Effect\": \"Allow\",
     \"Action\": [\"xray:PutTraceSegments\", \"xray:PutTelemetryRecords\"], \"Resource\": \"*\"},
    {\"Sid\": \"CWMetrics\", \"Effect\": \"Allow\", \"Action\": \"cloudwatch:PutMetricData\", \"Resource\": \"*\"}
  ]
}"
ROLE_ARN="arn:aws:iam::${ACCOUNT}:role/${ROLE_NAME}"

# ------------------------------------------------------------- 5. runtimes
# Pagination-safe: the CLI emits one text block per page, so aggregate and
# take the first non-empty token (empty -> "None").
runtime_id_for() {
    local id
    id=$(aws bedrock-agentcore-control list-agent-runtimes --region "$REGION" \
        --query "agentRuntimes[?agentRuntimeName=='$1'].agentRuntimeId" --output text \
        | awk 'NF && $1 != "None" {print $1; exit}')
    echo "${id:-None}"
}

for tag in "${TAGS[@]}"; do
    name="coldstart_ping_${tag}"
    rid=$(runtime_id_for "$name")
    if [ "$rid" != "None" ] && [ -n "$rid" ]; then
        current_uri=$(aws bedrock-agentcore-control get-agent-runtime --region "$REGION" \
            --agent-runtime-id "$rid" --query 'agentRuntimeArtifact.containerConfiguration.containerUri' --output text)
        if [ "$current_uri" = "$ECR_URI:$tag" ] && [ "${PUSHED[$tag]}" != "1" ]; then
            echo "Runtime $name exists with correct image — skipping create"
            continue
        fi
        echo "Runtime $name exists but image was re-pushed or URI changed ($current_uri) — updating to new version"
        aws bedrock-agentcore-control update-agent-runtime --region "$REGION" \
            --agent-runtime-id "$rid" \
            --cli-input-json "{
              \"agentRuntimeArtifact\": {\"containerConfiguration\": {\"containerUri\": \"$ECR_URI:$tag\"}},
              \"networkConfiguration\": {\"networkMode\": \"PUBLIC\"},
              \"protocolConfiguration\": {\"serverProtocol\": \"HTTP\"},
              \"roleArn\": \"$ROLE_ARN\",
              \"lifecycleConfiguration\": {\"idleRuntimeSessionTimeout\": $IDLE_TIMEOUT}
            }" >/dev/null
        continue
    fi
    echo "Creating runtime $name"
    created=0
    for attempt in 1 2 3 4 5 6; do
        if out=$(aws bedrock-agentcore-control create-agent-runtime --region "$REGION" --cli-input-json "{
              \"agentRuntimeName\": \"$name\",
              \"agentRuntimeArtifact\": {\"containerConfiguration\": {\"containerUri\": \"$ECR_URI:$tag\"}},
              \"networkConfiguration\": {\"networkMode\": \"PUBLIC\"},
              \"protocolConfiguration\": {\"serverProtocol\": \"HTTP\"},
              \"roleArn\": \"$ROLE_ARN\",
              \"lifecycleConfiguration\": {\"idleRuntimeSessionTimeout\": $IDLE_TIMEOUT}
            }" 2>&1); then
            echo "$out" | head -3
            created=1
            break
        fi
        if echo "$out" | grep -q "ValidationException"; then
            echo "FATAL: validation error creating $name:" >&2
            echo "$out" >&2
            exit 1
        fi
        echo "create attempt $attempt failed (likely IAM propagation), retrying in 10s: $(echo "$out" | head -1)"
        sleep 10
    done
    if (( ! created )); then
        echo "FATAL: could not create runtime $name after 6 attempts" >&2
        exit 1
    fi
done

# ------------------------------------------------------------- 6. poll READY
echo "Polling runtime status (timeout 10 min)..."
deadline=$(( $(date +%s) + 600 ))
declare -A RID ARN STATUS
while :; do
    all_ready=1
    line=""
    for tag in "${TAGS[@]}"; do
        name="coldstart_ping_${tag}"
        RID[$tag]=$(runtime_id_for "$name")
        read -r st arn < <(aws bedrock-agentcore-control get-agent-runtime --region "$REGION" \
            --agent-runtime-id "${RID[$tag]}" --query '[status, agentRuntimeArn]' --output text)
        STATUS[$tag]=$st
        ARN[$tag]=$arn
        line+="$name=$st  "
        if [ "$st" = "CREATE_FAILED" ] || [ "$st" = "UPDATE_FAILED" ]; then
            echo "FATAL: $name status $st; failureReason:" >&2
            aws bedrock-agentcore-control get-agent-runtime --region "$REGION" \
                --agent-runtime-id "${RID[$tag]}" --query 'failureReason' --output text >&2
            exit 1
        fi
        [ "$st" != "READY" ] && all_ready=0
    done
    echo "$(date +%T)  $line"
    (( all_ready )) && break
    if (( $(date +%s) > deadline )); then
        echo "FATAL: timed out waiting for READY" >&2
        exit 1
    fi
    sleep 10
done
echo "All runtimes READY."

# ------------------------------------------------- 7. write deployments.json
for tag in "${TAGS[@]}"; do
    export "DOCKER_${tag}"=$(docker image inspect --format '{{.Size}}' "$IMAGE:$tag")
    export "ECRB_${tag}"=$(aws ecr describe-images --repository-name "$REPO" --region "$REGION" \
        --image-ids imageTag="$tag" --query 'imageDetails[0].imageSizeInBytes' --output text)
    export "ARN_${tag}=${ARN[$tag]}"
done
export ACCOUNT REGION REPO ECR_URI
python3 - <<'EOF'
import json, os, datetime
tags = ["500mb", "1gb", "2gb"]
doc = {
    "region": os.environ["REGION"],
    "account": os.environ["ACCOUNT"],
    "ecr_repo": os.environ["ECR_URI"],
    "runtimes": {
        t: {
            "name": f"coldstart_ping_{t}",
            "arn": os.environ[f"ARN_{t}"],
            "image_uri": f"{os.environ['ECR_URI']}:{t}",
            "docker_size_bytes": int(os.environ[f"DOCKER_{t}"]),
            "ecr_size_bytes": int(os.environ[f"ECRB_{t}"]),
        }
        for t in tags
    },
    "iam_role": f"arn:aws:iam::{os.environ['ACCOUNT']}:role/AgentCoreColdstartRole",
    "deployed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
}
with open("deployments.json", "w") as f:
    json.dump(doc, f, indent=2)
print("Wrote deployments.json")
EOF
cat deployments.json
