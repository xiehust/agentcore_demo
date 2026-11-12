#!/usr/bin/env bash
# Idempotent deploy: private ECR repo + image push + least-privilege execution
# role + AgentCore runtimes, then poll READY and record everything in
# deployments.json. DEPLOY_KEYS="key ..." limits create/update to those keys
# (the others are only looked up), so a running test is not disturbed.
#
#   dsh_coldstart_base_v2     platform V2, stock DSH
#   dsh_coldstart_prewarm_v2  platform V2, DSH_PREWARM=1
#   dsh_coldstart_diag_v2     platform V2, pin image, DSH_PREWARM=0: control for the page-pin test
#   dsh_coldstart_deepwarm_v2 platform V2, pin image, DSH_PREWARM=3: page pin before listening
#                             (both keep the names they were first created with)
#
# Runtimes use the default inbound auth (SigV4 / IAM); nothing is public.
set -euo pipefail
cd "$(dirname "$0")/.."

REGION="${REGION:-us-east-1}"
DSH_VERSION="${DSH_VERSION:-0.2.2-rc.1}"
MODEL_ID="${MODEL_ID:-deepseek.v3.2}"
IMAGE="${IMAGE:-dsh-coldstart}"
TAG="${TAG:-dsh-${DSH_VERSION}}"
REPO=agentcore-dsh-coldstart
ROLE_NAME=AgentCoreDshColdstartRole
POLICY_NAME=AgentCoreDshColdstartPolicy
IDLE_TIMEOUT=60
MAX_IMAGE_BYTES=$((2048 * 1024 * 1024))   # AgentCore quota: max 2048 MB image
PIN_TAG="${PIN_TAG:-${TAG}-pin}"   # same DSH; adds the page pinner, its manifest and python3
KEYS=(dsh_v2 dsh_prewarm_v2 dsh_pinctl_v2 dsh_pin_v2)
declare -A NAME=([dsh_v2]=dsh_coldstart_base_v2 [dsh_prewarm_v2]=dsh_coldstart_prewarm_v2
                 [dsh_pinctl_v2]=dsh_coldstart_diag_v2 [dsh_pin_v2]=dsh_coldstart_deepwarm_v2)
declare -A PREWARM=([dsh_v2]=0 [dsh_prewarm_v2]=1 [dsh_pinctl_v2]=0 [dsh_pin_v2]=3)
declare -A PLATFORM=([dsh_v2]=V2 [dsh_prewarm_v2]=V2 [dsh_pinctl_v2]=V2 [dsh_pin_v2]=V2)
declare -A TAGOF=([dsh_v2]=$TAG [dsh_prewarm_v2]=$TAG
                  [dsh_pinctl_v2]=$PIN_TAG [dsh_pin_v2]=$PIN_TAG)
read -r -a TARGET_KEYS <<< "${DEPLOY_KEYS:-${KEYS[*]}}"

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
ECR_URI="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com/${REPO}"
echo "Account: $ACCOUNT  Region: $REGION  Repo: $ECR_URI  Model: $MODEL_ID  Targets: ${TARGET_KEYS[*]}"

# ---------------------------------------------------------------- 1. ECR repo
if aws ecr describe-repositories --repository-names "$REPO" --region "$REGION" >/dev/null 2>&1; then
    echo "ECR repo exists — skipping create"
else
    echo "Creating private ECR repo $REPO"
    aws ecr create-repository --repository-name "$REPO" --region "$REGION" \
        --image-scanning-configuration scanOnPush=true >/dev/null
fi
REPO_ARN="arn:aws:ecr:${REGION}:${ACCOUNT}:repository/${REPO}"

# -------------------------------------------------------------- 2. push image
aws ecr get-login-password --region "$REGION" \
    | docker login --username AWS --password-stdin "${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com" >/dev/null
declare -A PUSHED ECR_BYTES DOCKER_BYTES
for key in "${TARGET_KEYS[@]}"; do
    tag="${TAGOF[$key]}"
    [ -n "${PUSHED[$tag]:-}" ] && continue
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
for key in "${KEYS[@]}"; do
    tag="${TAGOF[$key]}"
    [ -n "${ECR_BYTES[$tag]:-}" ] && continue
    ECR_BYTES[$tag]=$(aws ecr describe-images --repository-name "$REPO" --region "$REGION" \
        --image-ids imageTag="$tag" --query 'imageDetails[0].imageSizeInBytes' --output text)
    DOCKER_BYTES[$tag]=$(docker image inspect --format '{{.Size}}' "$IMAGE:$tag")
    echo "ECR $tag imageSizeInBytes=${ECR_BYTES[$tag]} ($((ECR_BYTES[$tag] / 1024 / 1024)) MB compressed)"
    if (( ECR_BYTES[$tag] >= MAX_IMAGE_BYTES )); then
        echo "FATAL: image $tag is over the 2048 MB AgentCore quota" >&2
        exit 1
    fi
done

# ----------------------------------------------------------------- 3. IAM role
# Trust: only AgentCore, only for runtimes of this account and region.
TRUST=$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
    "Action": "sts:AssumeRole",
    "Condition": {
      "StringEquals": {"aws:SourceAccount": "$ACCOUNT"},
      "ArnLike": {"aws:SourceArn": "arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT}:*"}
    }
  }]
}
JSON
)
if aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
    echo "IAM role exists — refreshing trust policy"
    aws iam update-assume-role-policy --role-name "$ROLE_NAME" --policy-document "$TRUST"
else
    echo "Creating IAM role $ROLE_NAME"
    aws iam create-role --role-name "$ROLE_NAME" --assume-role-policy-document "$TRUST" >/dev/null
fi
# Permissions: pull this one image, write its own logs, call this one model.
aws iam put-role-policy --role-name "$ROLE_NAME" --policy-name "$POLICY_NAME" --policy-document "$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {"Sid": "ECRAuth", "Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*"},
    {"Sid": "ECRPull", "Effect": "Allow",
     "Action": ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"],
     "Resource": "$REPO_ARN"},
    {"Sid": "LogGroups", "Effect": "Allow",
     "Action": ["logs:CreateLogGroup", "logs:DescribeLogGroups", "logs:DescribeLogStreams"],
     "Resource": "arn:aws:logs:${REGION}:${ACCOUNT}:log-group:*"},
    {"Sid": "LogEvents", "Effect": "Allow",
     "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
     "Resource": "arn:aws:logs:${REGION}:${ACCOUNT}:log-group:/aws/bedrock-agentcore/runtimes/*"},
    {"Sid": "InvokeDeepSeek", "Effect": "Allow",
     "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
     "Resource": "arn:aws:bedrock:${REGION}::foundation-model/${MODEL_ID}"}
  ]
}
JSON
)"
ROLE_ARN="arn:aws:iam::${ACCOUNT}:role/${ROLE_NAME}"

# ------------------------------------------------------------- 4. runtimes
runtime_id_for() {
    local id
    id=$(aws bedrock-agentcore-control list-agent-runtimes --region "$REGION" \
        --query "agentRuntimes[?agentRuntimeName=='$1'].agentRuntimeId" --output text \
        | awk 'NF && $1 != "None" {print $1; exit}')
    echo "${id:-None}"
}
runtime_config() {   # $1 = key
    cat <<JSON
  "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": "$ECR_URI:${TAGOF[$1]}"}},
  "networkConfiguration": {"networkMode": "PUBLIC"},
  "protocolConfiguration": {"serverProtocol": "HTTP"},
  "roleArn": "$ROLE_ARN",
  "platformVersion": "${PLATFORM[$1]}",
  "environmentVariables": {"DSH_MODEL_ID": "$MODEL_ID", "DSH_PREWARM": "${PREWARM[$1]}"},
  "lifecycleConfiguration": {"idleRuntimeSessionTimeout": $IDLE_TIMEOUT}
JSON
}

for key in "${TARGET_KEYS[@]}"; do
    name="${NAME[$key]}"
    tag="${TAGOF[$key]}"
    rid=$(runtime_id_for "$name")
    if [ "$rid" != "None" ]; then
        current_uri=$(aws bedrock-agentcore-control get-agent-runtime --region "$REGION" \
            --agent-runtime-id "$rid" --query 'agentRuntimeArtifact.containerConfiguration.containerUri' --output text)
        if [ "$current_uri" = "$ECR_URI:$tag" ] && [ "${PUSHED[$tag]}" != "1" ]; then
            echo "Runtime $name exists with this image — skipping"
            continue
        fi
        echo "Runtime $name exists; image changed — updating"
        aws bedrock-agentcore-control update-agent-runtime --region "$REGION" \
            --agent-runtime-id "$rid" --cli-input-json "{ $(runtime_config "$key") }" >/dev/null
        continue
    fi
    echo "Creating runtime $name"
    created=0
    for attempt in 1 2 3 4 5 6; do
        if out=$(aws bedrock-agentcore-control create-agent-runtime --region "$REGION" \
            --cli-input-json "{ \"agentRuntimeName\": \"$name\", $(runtime_config "$key") }" 2>&1); then
            created=1
            break
        fi
        if echo "$out" | grep -q "ValidationException" && ! echo "$out" | grep -qi "role"; then
            echo "FATAL: validation error creating $name:" >&2
            echo "$out" >&2
            exit 1
        fi
        echo "create attempt $attempt failed (likely IAM propagation), retrying in 10s: $(echo "$out" | tail -1)"
        sleep 10
    done
    (( created )) || { echo "FATAL: could not create runtime $name" >&2; exit 1; }
done

# ------------------------------------------------------------- 5. poll READY
echo "Polling runtime status (timeout 10 min)..."
deadline=$(( $(date +%s) + 600 ))
declare -A ARN CREATED
while :; do
    all_ready=1
    line=""
    for key in "${KEYS[@]}"; do
        rid=$(runtime_id_for "${NAME[$key]}")
        read -r st arn pv created < <(aws bedrock-agentcore-control get-agent-runtime --region "$REGION" \
            --agent-runtime-id "$rid" --query '[status, agentRuntimeArn, platformVersion, lastUpdatedAt]' --output text)
        ARN[$key]=$arn
        CREATED[$key]=$created
        line+="${NAME[$key]}=$st($pv)  "
        if [ "$st" = "READY" ] && [ "$pv" != "${PLATFORM[$key]}" ]; then
            echo "FATAL: ${NAME[$key]} reports platformVersion=$pv, expected ${PLATFORM[$key]}" >&2
            exit 1
        fi
        if [ "$st" = "CREATE_FAILED" ] || [ "$st" = "UPDATE_FAILED" ]; then
            echo "FATAL: ${NAME[$key]} status $st:" >&2
            aws bedrock-agentcore-control get-agent-runtime --region "$REGION" \
                --agent-runtime-id "$rid" --query 'failureReason' --output text >&2
            exit 1
        fi
        [ "$st" != "READY" ] && all_ready=0
    done
    echo "$(date +%T)  $line"
    (( all_ready )) && break
    (( $(date +%s) > deadline )) && { echo "FATAL: timed out waiting for READY" >&2; exit 1; }
    sleep 10
done

# ------------------------------------------------- 6. write deployments.json
export ACCOUNT REGION ECR_URI MODEL_ID DSH_VERSION ROLE_ARN
export KEY_LIST="${KEYS[*]}"
for key in "${KEYS[@]}"; do
    tag="${TAGOF[$key]}"
    export "ARN_${key}=${ARN[$key]}" "NAME_${key}=${NAME[$key]}" "PREWARM_${key}=${PREWARM[$key]}" \
        "PLATFORM_${key}=${PLATFORM[$key]}" "TAG_${key}=${tag}" "CREATED_${key}=${CREATED[$key]}" \
        "ECRB_${key}=${ECR_BYTES[$tag]}" "DOCKERB_${key}=${DOCKER_BYTES[$tag]}"
done
python3 - <<'EOF'
import datetime, json, os
e = os.environ
now = datetime.datetime.now(datetime.timezone.utc).isoformat()
# Runtimes that were measured and later deleted stay on record, so their raw
# results can still be labelled.
try:
    removed = json.load(open("deployments.json")).get("removed_runtimes", {})
except (OSError, ValueError):
    removed = {}
doc = {
    "region": e["REGION"],
    "account": e["ACCOUNT"],
    "ecr_repo": e["ECR_URI"],
    "model_id": e["MODEL_ID"],
    "dsh_version": e["DSH_VERSION"],
    "runtimes": {
        key: {
            "name": e[f"NAME_{key}"],
            "arn": e[f"ARN_{key}"],
            "image_uri": f"{e['ECR_URI']}:{e[f'TAG_{key}']}",
            "docker_size_bytes": int(e[f"DOCKERB_{key}"]),
            "ecr_size_bytes": int(e[f"ECRB_{key}"]),
            "platform_version": e[f"PLATFORM_{key}"],
            "env": {"DSH_MODEL_ID": e["MODEL_ID"], "DSH_PREWARM": e[f"PREWARM_{key}"]},
            "deployed_at": e[f"CREATED_{key}"],   # lastUpdatedAt: when this version (and its V2 snapshot) was made
        }
        for key in e["KEY_LIST"].split()
    },
    "removed_runtimes": removed,
    "iam_role": e["ROLE_ARN"],
    "deployed_at": now,
}
with open("deployments.json", "w") as f:
    json.dump(doc, f, indent=2)
print("Wrote deployments.json")
EOF
