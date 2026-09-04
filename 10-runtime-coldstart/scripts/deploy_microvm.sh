#!/usr/bin/env bash
# Idempotent deploy for the Lambda MicroVMs track: S3 artifact bucket + build /
# execution IAM roles + three MicroVM images (coldstart-ping-microvm-500mb|1gb|2gb,
# size-calibrated like the AgentCore images), then poll the builds and record
# everything in deployments_microvm.json.
#
# Lambda builds the Dockerfile itself from a zip in S3 and snapshots the started
# app, so there is no local image push. Docker is still used once to measure the
# unpadded base image size so the pad layers hit the same 500/1024/1950 MB targets
# as scripts/build_images.sh.
#
# Usage: scripts/deploy_microvm.sh            # create or reuse
#        scripts/deploy_microvm.sh --rebuild  # force a new image version even if unchanged
set -euo pipefail
cd "$(dirname "$0")/.."

REGION=us-west-2
NAME_PREFIX=coldstart-ping-microvm
BUILD_ROLE_NAME=LambdaMicrovmColdstartBuildRole
EXEC_ROLE_NAME=LambdaMicrovmColdstartExecRole
POLICY_NAME=LambdaMicrovmColdstartPolicy
BASE_IMAGE_ARN="arn:aws:lambda:${REGION}:aws:microvm-image:al2023-1"
MEMORY_MIB=2048                     # baseline 2 GB / 1 vCPU (service default)
LOCAL_IMAGE=coldstart-pingpong-microvm
TAGS=(500mb 1gb 2gb)
declare -A TARGET=( [500mb]=500 [1gb]=1024 [2gb]=1950 )   # MB, same as build_images.sh
FORCE_REBUILD=0
[ "${1:-}" = "--rebuild" ] && FORCE_REBUILD=1

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
BUCKET="lambda-microvm-coldstart-${ACCOUNT}-${REGION}"
echo "Account: $ACCOUNT  Region: $REGION  Bucket: s3://$BUCKET"

# ------------------------------------------------ 0. region + base image check
aws lambda-microvms list-managed-microvm-images --region "$REGION" \
    --query "items[?imageArn=='${BASE_IMAGE_ARN}'].imageArn" --output text | grep -q . \
    || { echo "FATAL: managed base image $BASE_IMAGE_ARN not found — is Lambda MicroVMs available in $REGION?" >&2; exit 1; }

# ------------------------------------------------ 1. measure unpadded base size
echo "== Measuring unpadded base image size (docker, linux/arm64) =="
docker build --platform linux/arm64 -f microvm/Dockerfile -t "$LOCAL_IMAGE:local" microvm/ >/dev/null
BASE_BYTES=$(docker image inspect --format '{{.Size}}' "$LOCAL_IMAGE:local")
BASE_MB=$(( BASE_BYTES / 1024 / 1024 ))
echo "Base image: ${BASE_MB} MB (${BASE_BYTES} bytes)"

# ---------------------------------------------------------------- 2. S3 bucket
if aws s3api head-bucket --bucket "$BUCKET" --region "$REGION" >/dev/null 2>&1; then
    echo "S3 bucket exists — skipping create"
else
    echo "Creating S3 bucket $BUCKET"
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
        --create-bucket-configuration LocationConstraint="$REGION" >/dev/null
    aws s3api put-public-access-block --bucket "$BUCKET" --region "$REGION" \
        --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
fi

# ---------------------------------------------------------------- 3. IAM roles
TRUST_DOC="{
  \"Version\": \"2012-10-17\",
  \"Statement\": [{
    \"Effect\": \"Allow\",
    \"Principal\": {\"Service\": \"lambda.amazonaws.com\"},
    \"Action\": \"sts:AssumeRole\",
    \"Condition\": {\"StringEquals\": {\"aws:SourceAccount\": \"$ACCOUNT\"}}
  }]
}"
ensure_role() {  # name policy-json
    if aws iam get-role --role-name "$1" >/dev/null 2>&1; then
        echo "IAM role $1 exists — skipping create"
    else
        echo "Creating IAM role $1"
        aws iam create-role --role-name "$1" --assume-role-policy-document "$TRUST_DOC" >/dev/null
    fi
    aws iam put-role-policy --role-name "$1" --policy-name "$POLICY_NAME" --policy-document "$2"
}
LOGS_STMT="{\"Sid\": \"Logs\", \"Effect\": \"Allow\",
     \"Action\": [\"logs:CreateLogGroup\", \"logs:CreateLogStream\", \"logs:PutLogEvents\", \"logs:DescribeLogStreams\", \"logs:DescribeLogGroups\"],
     \"Resource\": \"arn:aws:logs:${REGION}:${ACCOUNT}:*\"}"
ensure_role "$BUILD_ROLE_NAME" "{
  \"Version\": \"2012-10-17\",
  \"Statement\": [
    {\"Sid\": \"Artifact\", \"Effect\": \"Allow\", \"Action\": [\"s3:GetObject\", \"s3:GetObjectVersion\"],
     \"Resource\": \"arn:aws:s3:::${BUCKET}/*\"},
    $LOGS_STMT
  ]
}"
ensure_role "$EXEC_ROLE_NAME" "{
  \"Version\": \"2012-10-17\",
  \"Statement\": [ $LOGS_STMT ]
}"
BUILD_ROLE_ARN="arn:aws:iam::${ACCOUNT}:role/${BUILD_ROLE_NAME}"
EXEC_ROLE_ARN="arn:aws:iam::${ACCOUNT}:role/${EXEC_ROLE_NAME}"

# ------------------------------------------- 4. package + upload code artifacts
# CreateMicrovmImage has no build-args, so the PADn_MB defaults are rewritten
# into a per-variant copy of the Dockerfile (<=500 MB urandom chunks, one layer
# each, same scheme as build_images.sh).
STAGE=.build/microvm
rm -rf "$STAGE" && mkdir -p "$STAGE"
declare -A PAD_MB ARTIFACT_URI ARTIFACT_SHA
for tag in "${TAGS[@]}"; do
    pad_total=$(( TARGET[$tag] - BASE_MB ))
    if (( pad_total <= 0 )); then
        echo "FATAL: target ${TARGET[$tag]}MB <= base ${BASE_MB}MB for $tag" >&2; exit 1
    fi
    PAD_MB[$tag]=$pad_total
    dir="$STAGE/$tag"; mkdir -p "$dir"
    cp microvm/app.py "$dir/app.py"
    rem=$pad_total; sed_expr=""
    for i in 1 2 3 4; do
        chunk=$(( rem >= 500 ? 500 : rem )); (( chunk < 0 )) && chunk=0
        sed_expr+="s/^ARG PAD${i}_MB=0$/ARG PAD${i}_MB=${chunk}/;"
        rem=$(( rem - chunk ))
    done
    (( rem == 0 )) || { echo "FATAL: pad ${pad_total}MB for $tag exceeds 4x500MB chunks" >&2; exit 1; }
    sed -e "$sed_expr" microvm/Dockerfile > "$dir/Dockerfile"
    grep -q "ARG PAD1_MB=$(( pad_total >= 500 ? 500 : pad_total ))" "$dir/Dockerfile" \
        || { echo "FATAL: pad substitution failed for $tag" >&2; exit 1; }
    sha=$(cat "$dir/Dockerfile" "$dir/app.py" | sha256sum | cut -c1-16)
    ARTIFACT_SHA[$tag]=$sha
    (cd "$dir" && rm -f artifact.zip && zip -q -X artifact.zip Dockerfile app.py)
    key="microvm-images/${tag}/${sha}.zip"
    ARTIFACT_URI[$tag]="s3://${BUCKET}/${key}"
    if aws s3api head-object --bucket "$BUCKET" --key "$key" --region "$REGION" >/dev/null 2>&1; then
        echo "Artifact $tag ($sha, pad ${pad_total}MB) already in S3 — skipping upload"
    else
        echo "Uploading artifact $tag ($sha, pad ${pad_total}MB) -> ${ARTIFACT_URI[$tag]}"
        aws s3 cp "$dir/artifact.zip" "${ARTIFACT_URI[$tag]}" --region "$REGION" --only-show-errors
    fi
done

# --------------------------------------------------------- 5. MicroVM images
HOOKS='{
  "port": 9000,
  "microvmImageHooks": {"ready": "ENABLED", "readyTimeoutInSeconds": 120,
                        "validate": "ENABLED", "validateTimeoutInSeconds": 60},
  "microvmHooks": {"run": "ENABLED", "runTimeoutInSeconds": 5,
                   "resume": "ENABLED", "resumeTimeoutInSeconds": 5,
                   "suspend": "ENABLED", "suspendTimeoutInSeconds": 5,
                   "terminate": "ENABLED", "terminateTimeoutInSeconds": 5}
}'
declare -A IMAGE_ARN IMAGE_VERSION
for tag in "${TAGS[@]}"; do
    name="${NAME_PREFIX}-${tag}"
    arn="arn:aws:lambda:${REGION}:${ACCOUNT}:microvm-image:${name}"
    IMAGE_ARN[$tag]=$arn
    if existing=$(aws lambda-microvms get-microvm-image --region "$REGION" --image-identifier "$arn" \
            --query '[state, latestActiveImageVersion]' --output text 2>/dev/null); then
        read -r st ver <<<"$existing"
        cur_uri="None"
        if [ "$ver" != "None" ] && [ -n "$ver" ]; then
            cur_uri=$(aws lambda-microvms get-microvm-image-version --region "$REGION" \
                --image-identifier "$arn" --image-version "$ver" --query 'codeArtifact.uri' --output text)
        fi
        if [ "$cur_uri" = "${ARTIFACT_URI[$tag]}" ] && (( ! FORCE_REBUILD )); then
            echo "Image $name exists (state $st, version $ver) with current artifact — skipping"
            IMAGE_VERSION[$tag]=$ver
            continue
        fi
        echo "Image $name exists (artifact $cur_uri) — building new version from ${ARTIFACT_URI[$tag]}"
        IMAGE_VERSION[$tag]=$(aws lambda-microvms update-microvm-image --region "$REGION" \
            --image-identifier "$arn" \
            --base-image-arn "$BASE_IMAGE_ARN" \
            --build-role-arn "$BUILD_ROLE_ARN" \
            --code-artifact "{\"uri\": \"${ARTIFACT_URI[$tag]}\"}" \
            --description "coldstart ping-pong, target ${TARGET[$tag]}MB (pad ${PAD_MB[$tag]}MB), artifact ${ARTIFACT_SHA[$tag]}" \
            --query 'imageVersion' --output text)
        continue
    fi
    echo "Creating MicroVM image $name"
    created=0
    for attempt in 1 2 3 4 5 6; do
        if out=$(aws lambda-microvms create-microvm-image --region "$REGION" \
                --name "$name" \
                --description "coldstart ping-pong, target ${TARGET[$tag]}MB (pad ${PAD_MB[$tag]}MB), artifact ${ARTIFACT_SHA[$tag]}" \
                --base-image-arn "$BASE_IMAGE_ARN" \
                --build-role-arn "$BUILD_ROLE_ARN" \
                --code-artifact "{\"uri\": \"${ARTIFACT_URI[$tag]}\"}" \
                --resources "[{\"minimumMemoryInMiB\": $MEMORY_MIB}]" \
                --cpu-configurations '[{"architecture": "ARM_64"}]' \
                --hooks "$HOOKS" \
                --query '[imageArn, imageVersion, state]' --output text 2>&1); then
            echo "  $out"
            IMAGE_VERSION[$tag]=$(awk '{print $2}' <<<"$out")
            created=1
            break
        fi
        if grep -q "ValidationException" <<<"$out"; then
            echo "FATAL: validation error creating $name:" >&2; echo "$out" >&2; exit 1
        fi
        echo "  create attempt $attempt failed (likely IAM propagation), retrying in 10s: $(head -1 <<<"$out")"
        sleep 10
    done
    (( created )) || { echo "FATAL: could not create image $name after 6 attempts" >&2; exit 1; }
done

# -------------------------------------------------------------- 6. poll builds
echo "Polling image builds (timeout 45 min; a 2 GB urandom pad takes a while)..."
deadline=$(( $(date +%s) + 2700 ))
while :; do
    all_done=1; line=""
    for tag in "${TAGS[@]}"; do
        ver="${IMAGE_VERSION[$tag]}"
        if [ -z "$ver" ] || [ "$ver" = "None" ]; then
            ver=$(aws lambda-microvms get-microvm-image --region "$REGION" \
                --image-identifier "${IMAGE_ARN[$tag]}" --query 'latestActiveImageVersion' --output text)
            IMAGE_VERSION[$tag]=$ver
        fi
        read -r vstate vstatus reason < <(aws lambda-microvms get-microvm-image-version --region "$REGION" \
            --image-identifier "${IMAGE_ARN[$tag]}" --image-version "$ver" \
            --query '[state, status, stateReason]' --output text)
        line+="${tag}:v${ver}=${vstate}  "
        if [ "$vstate" = "FAILED" ]; then
            echo "FATAL: image ${NAME_PREFIX}-${tag} version $ver FAILED: $reason" >&2
            echo "       build logs: CloudWatch log group /aws/lambda/microvms/${NAME_PREFIX}-${tag}" >&2
            exit 1
        fi
        [ "$vstate" = "SUCCESSFUL" ] || all_done=0
    done
    echo "$(date +%T)  $line"
    (( all_done )) && break
    (( $(date +%s) > deadline )) && { echo "FATAL: timed out waiting for image builds" >&2; exit 1; }
    sleep 20
done
echo "All MicroVM images built."

# ------------------------------------------- 7. write deployments_microvm.json
for tag in "${TAGS[@]}"; do
    export "ARN_${tag}=${IMAGE_ARN[$tag]}" "VER_${tag}=${IMAGE_VERSION[$tag]}" \
           "URI_${tag}=${ARTIFACT_URI[$tag]}" "SHA_${tag}=${ARTIFACT_SHA[$tag]}" \
           "PAD_${tag}=${PAD_MB[$tag]}" "TGT_${tag}=${TARGET[$tag]}"
done
export ACCOUNT REGION BUCKET BASE_IMAGE_ARN BASE_BYTES MEMORY_MIB BUILD_ROLE_ARN EXEC_ROLE_ARN NAME_PREFIX
python3 - <<'EOF'
import json, os, datetime
tags = ["500mb", "1gb", "2gb"]
doc = {
    "region": os.environ["REGION"],
    "account": os.environ["ACCOUNT"],
    "bucket": os.environ["BUCKET"],
    "base_image_arn": os.environ["BASE_IMAGE_ARN"],
    "base_docker_size_bytes": int(os.environ["BASE_BYTES"]),
    "memory_mib": int(os.environ["MEMORY_MIB"]),
    "build_role": os.environ["BUILD_ROLE_ARN"],
    "execution_role": os.environ["EXEC_ROLE_ARN"],
    "images": {
        t: {
            "name": f"{os.environ['NAME_PREFIX']}-{t}",
            "arn": os.environ[f"ARN_{t}"],
            "version": os.environ[f"VER_{t}"],
            "artifact_uri": os.environ[f"URI_{t}"],
            "artifact_sha256_16": os.environ[f"SHA_{t}"],
            "target_mb": int(os.environ[f"TGT_{t}"]),
            "pad_mb": int(os.environ[f"PAD_{t}"]),
        }
        for t in tags
    },
    "deployed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
}
with open("deployments_microvm.json", "w") as f:
    json.dump(doc, f, indent=2)
print("Wrote deployments_microvm.json")
EOF
cat deployments_microvm.json
