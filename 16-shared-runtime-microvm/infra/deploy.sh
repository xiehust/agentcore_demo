#!/usr/bin/env bash
# Deploy the shared-runtime session-pool demo with plain aws-cli calls.
#
#   1. IAM roles (runtime, ECS exec, router task, reconciler Lambda)
#   2. DynamoDB single table + pool GSI + TTL
#   3. ECR images (agent runtime + router), linux/arm64
#   4. VPC plumbing for the Runtime (private subnets + NAT, or reuse via
#      RUNTIME_SUBNET_IDS), S3 bucket + S3 Files file system / mount targets /
#      access point, AgentCore Runtime in VPC mode with S3 Files at /mnt/users
#   5. Security groups + internal ALB (client CIDR only) + target group
#   6. ECS Fargate cluster / task definition / service for the router
#   7. Reconciler Lambda + EventBridge schedule
#   8. pool.json with every identifier the client needs
#
# Every step is idempotent: re-running updates in place.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/env.sh"
cd "${ROOT}"

for tool in aws docker python3 jq zip uv; do
  command -v "${tool}" >/dev/null || die "missing required tool: ${tool}"
done
[[ -n "${CLIENT_CIDR}" ]] || die "CLIENT_CIDR is required (e.g. 172.31.30.139/32); could not auto-detect"

# ------------------------------------------------------------------ helpers
role_arn() { aws iam get-role --role-name "$1" --query Role.Arn --output text; }

ensure_role() {  # name trust-json
  local name="$1" trust="$2"
  if aws iam get-role --role-name "${name}" >/dev/null 2>&1; then
    aws iam update-assume-role-policy --role-name "${name}" --policy-document "${trust}"
  else
    aws iam create-role --role-name "${name}" --assume-role-policy-document "${trust}" \
      --description "shared-runtime session pool demo (${PREFIX})" >/dev/null
    echo "created role ${name}"
  fi
}

retry() {  # attempts delay cmd...
  local attempts="$1" delay="$2"; shift 2
  local i
  for ((i = 1; i <= attempts; i++)); do
    if "$@"; then return 0; fi
    echo "  attempt ${i}/${attempts} failed; retrying in ${delay}s" >&2
    sleep "${delay}"
  done
  return 1
}

# ------------------------------------------------------------ 1. IAM roles
log "[1/8] IAM roles"
TRUST_AGENTCORE="$(python3 - "${ACCOUNT_ID}" "${REGION}" <<'PY'
import json, sys
account, region = sys.argv[1:]
print(json.dumps({"Version": "2012-10-17", "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
    "Action": "sts:AssumeRole",
    "Condition": {
        "StringEquals": {"aws:SourceAccount": account},
        "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{region}:{account}:*"},
    },
}]}))
PY
)"
TRUST_ECS='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
TRUST_LAMBDA='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'

ensure_role "${RUNTIME_ROLE_NAME}" "${TRUST_AGENTCORE}"
ensure_role "${ECS_EXEC_ROLE_NAME}" "${TRUST_ECS}"
ensure_role "${ROUTER_TASK_ROLE_NAME}" "${TRUST_ECS}"
ensure_role "${LAMBDA_ROLE_NAME}" "${TRUST_LAMBDA}"

aws iam put-role-policy --role-name "${RUNTIME_ROLE_NAME}" --policy-name RuntimeExecution \
  --policy-document "$(python3 - "${ACCOUNT_ID}" "${REGION}" "${REPO}" <<'PY'
import json, sys
account, region, repo = sys.argv[1:]
print(json.dumps({"Version": "2012-10-17", "Statement": [
    {"Sid": "ECRImageAccess", "Effect": "Allow",
     "Action": ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
     "Resource": f"arn:aws:ecr:{region}:{account}:repository/{repo}"},
    {"Sid": "ECRToken", "Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*"},
    {"Effect": "Allow", "Action": ["logs:DescribeLogStreams", "logs:CreateLogGroup"],
     "Resource": f"arn:aws:logs:{region}:{account}:log-group:/aws/bedrock-agentcore/runtimes/*"},
    {"Effect": "Allow", "Action": "logs:DescribeLogGroups",
     "Resource": f"arn:aws:logs:{region}:{account}:log-group:*"},
    {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
     "Resource": f"arn:aws:logs:{region}:{account}:log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*"},
    {"Effect": "Allow", "Action": ["xray:PutTraceSegments", "xray:PutTelemetryRecords",
                                   "xray:GetSamplingRules", "xray:GetSamplingTargets"], "Resource": "*"},
    {"Effect": "Allow", "Action": "cloudwatch:PutMetricData", "Resource": "*",
     "Condition": {"StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}}},
    {"Sid": "WorkloadIdentity", "Effect": "Allow",
     "Action": ["bedrock-agentcore:GetWorkloadAccessToken", "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
                "bedrock-agentcore:GetWorkloadAccessTokenForUserId"],
     "Resource": [f"arn:aws:bedrock-agentcore:{region}:{account}:workload-identity-directory/default",
                  f"arn:aws:bedrock-agentcore:{region}:{account}:workload-identity-directory/default/workload-identity/*"]},
    {"Sid": "BedrockModelInvocation", "Effect": "Allow",
     "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
     "Resource": ["arn:aws:bedrock:*::foundation-model/*", "arn:aws:bedrock:*:*:inference-profile/*"]},
]}))
PY
)"
aws iam attach-role-policy --role-name "${ECS_EXEC_ROLE_NAME}" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
aws iam attach-role-policy --role-name "${LAMBDA_ROLE_NAME}" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
RUNTIME_ROLE_ARN="$(role_arn "${RUNTIME_ROLE_NAME}")"
ECS_EXEC_ROLE_ARN="$(role_arn "${ECS_EXEC_ROLE_NAME}")"
ROUTER_TASK_ROLE_ARN="$(role_arn "${ROUTER_TASK_ROLE_NAME}")"
LAMBDA_ROLE_ARN="$(role_arn "${LAMBDA_ROLE_NAME}")"

# -------------------------------------------------------------- 2. DynamoDB
log "[2/8] DynamoDB table ${TABLE_NAME}"
if ! aws dynamodb describe-table --table-name "${TABLE_NAME}" --region "${REGION}" >/dev/null 2>&1; then
  aws dynamodb create-table --region "${REGION}" --table-name "${TABLE_NAME}" \
    --billing-mode PAY_PER_REQUEST \
    --attribute-definitions AttributeName=PK,AttributeType=S AttributeName=SK,AttributeType=S \
      AttributeName=GSI1PK,AttributeType=S AttributeName=GSI1SK,AttributeType=S \
    --key-schema AttributeName=PK,KeyType=HASH AttributeName=SK,KeyType=RANGE \
    --global-secondary-indexes '[{"IndexName":"GSI1","KeySchema":[{"AttributeName":"GSI1PK","KeyType":"HASH"},{"AttributeName":"GSI1SK","KeyType":"RANGE"}],"Projection":{"ProjectionType":"ALL"}}]' \
    --tags Key=project,Value="${PREFIX}" >/dev/null
  echo "created table"
fi
aws dynamodb wait table-exists --table-name "${TABLE_NAME}" --region "${REGION}"
TTL_STATUS="$(aws dynamodb describe-time-to-live --table-name "${TABLE_NAME}" --region "${REGION}" \
  --query TimeToLiveDescription.TimeToLiveStatus --output text)"
if [[ "${TTL_STATUS}" != "ENABLED" && "${TTL_STATUS}" != "ENABLING" ]]; then
  aws dynamodb update-time-to-live --table-name "${TABLE_NAME}" --region "${REGION}" \
    --time-to-live-specification Enabled=true,AttributeName=ttl >/dev/null
fi
TABLE_ARN="$(aws dynamodb describe-table --table-name "${TABLE_NAME}" --region "${REGION}" --query Table.TableArn --output text)"

# -------------------------------------------------------------- 3. images
log "[3/8] container images"
aws ecr describe-repositories --repository-names "${REPO}" --region "${REGION}" >/dev/null 2>&1 \
  || aws ecr create-repository --repository-name "${REPO}" --region "${REGION}" \
       --image-scanning-configuration scanOnPush=true >/dev/null
if [[ "${SKIP_IMAGE_BUILD}" == "1" ]]; then
  aws ecr describe-images --repository-name "${REPO}" --image-ids imageTag="${RUNTIME_TAG}" --region "${REGION}" >/dev/null
  aws ecr describe-images --repository-name "${REPO}" --image-ids imageTag="${ROUTER_TAG}" --region "${REGION}" >/dev/null
  echo "using existing images"
else
  aws ecr get-login-password --region "${REGION}" | docker login --username AWS --password-stdin "${ECR_HOST}"
  docker build --platform linux/arm64 -f docker/Dockerfile -t "${RUNTIME_IMAGE_URI}" .
  docker push "${RUNTIME_IMAGE_URI}"
  docker build --platform linux/arm64 -f docker/Dockerfile.router -t "${ROUTER_IMAGE_URI}" .
  docker push "${ROUTER_IMAGE_URI}"
fi

# ------------------------------------------------- 4a. VPC for the Runtime
# S3 Files requires networkMode=VPC. AgentCore ENIs in a public subnet get no
# internet access, so the Runtime lives in private subnets routed through a NAT
# gateway (Bedrock, ECR and CloudWatch egress).
log "[4/8] VPC: private subnets + NAT for the Runtime, S3 Files, Runtime"
if [[ -z "${VPC_ID}" ]]; then
  VPC_ID="$(aws ec2 describe-vpcs --region "${REGION}" --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text)"
fi
[[ "${VPC_ID}" != "None" && -n "${VPC_ID}" ]] || die "no VPC found; set VPC_ID"
if [[ -z "${SUBNET_IDS}" ]]; then   # public subnets (ALB, router tasks, NAT gateway)
  SUBNET_IDS="$(aws ec2 describe-subnets --region "${REGION}" \
    --filters Name=vpc-id,Values="${VPC_ID}" Name=default-for-az,Values=true \
    --query 'Subnets[].SubnetId' --output text | tr '\t' ',')"
fi
IFS=',' read -r -a SUBNET_ARR <<<"${SUBNET_IDS}"
[[ "${#SUBNET_ARR[@]}" -ge 2 ]] || die "need >=2 public subnets in different AZs; set SUBNET_IDS"
IGW_ID="$(aws ec2 describe-internet-gateways --region "${REGION}" --filters Name=attachment.vpc-id,Values="${VPC_ID}" \
  --query 'InternetGateways[0].InternetGatewayId' --output text)"
[[ "${IGW_ID}" != "None" ]] || die "VPC ${VPC_ID} has no internet gateway"

ensure_sg() {  # name description [vpc] -> id
  local name="$1" desc="$2" vpc="${3:-${VPC_ID}}" id
  id="$(aws ec2 describe-security-groups --region "${REGION}" \
    --filters Name=vpc-id,Values="${vpc}" Name=group-name,Values="${name}" \
    --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || true)"
  if [[ -z "${id}" || "${id}" == "None" ]]; then
    id="$(aws ec2 create-security-group --region "${REGION}" --vpc-id "${vpc}" \
      --group-name "${name}" --description "${desc}" --query GroupId --output text)"
    aws ec2 create-tags --region "${REGION}" --resources "${id}" --tags Key=project,Value="${PREFIX}"
  fi
  echo "${id}"
}
tag_name() { aws ec2 create-tags --region "${REGION}" --resources "$1" --tags Key=Name,Value="$2" Key=project,Value="${PREFIX}"; }

PRIVATE_SUBNETS=()
if [[ -n "${RUNTIME_SUBNET_IDS}" ]]; then
  IFS=',' read -r -a PRIVATE_SUBNETS <<<"${RUNTIME_SUBNET_IDS}"
  RUNTIME_VPC_ID="$(aws ec2 describe-subnets --region "${REGION}" --subnet-ids "${PRIVATE_SUBNETS[0]}" --query 'Subnets[0].VpcId' --output text)"
  NAT_ID="(external)"
  echo "reusing private subnets ${RUNTIME_SUBNET_IDS} in ${RUNTIME_VPC_ID}"
else
RUNTIME_VPC_ID="${VPC_ID}"
# NAT gateway in the first public subnet (idempotent by Name tag).
NAT_ID="$(aws ec2 describe-nat-gateways --region "${REGION}" \
  --filter Name=vpc-id,Values="${VPC_ID}" Name=tag:Name,Values="${PREFIX}-nat" Name=state,Values=pending,available \
  --query 'NatGateways[0].NatGatewayId' --output text)"
if [[ -z "${NAT_ID}" || "${NAT_ID}" == "None" ]]; then
  EIP_ALLOC="$(aws ec2 describe-addresses --region "${REGION}" --filters Name=tag:Name,Values="${PREFIX}-nat-eip" \
    --query 'Addresses[0].AllocationId' --output text)"
  if [[ -z "${EIP_ALLOC}" || "${EIP_ALLOC}" == "None" ]]; then
    EIP_ALLOC="$(aws ec2 allocate-address --region "${REGION}" --domain vpc --query AllocationId --output text)"
    tag_name "${EIP_ALLOC}" "${PREFIX}-nat-eip"
  fi
  NAT_ID="$(aws ec2 create-nat-gateway --region "${REGION}" --subnet-id "${SUBNET_ARR[0]}" \
    --allocation-id "${EIP_ALLOC}" --query NatGateway.NatGatewayId --output text)"
  tag_name "${NAT_ID}" "${PREFIX}-nat"
  echo "created NAT gateway ${NAT_ID}"
fi
aws ec2 wait nat-gateway-available --region "${REGION}" --nat-gateway-ids "${NAT_ID}"

# Private route table -> NAT.
PRIVATE_RTB="$(aws ec2 describe-route-tables --region "${REGION}" \
  --filters Name=vpc-id,Values="${VPC_ID}" Name=tag:Name,Values="${PREFIX}-private-rtb" \
  --query 'RouteTables[0].RouteTableId' --output text)"
if [[ -z "${PRIVATE_RTB}" || "${PRIVATE_RTB}" == "None" ]]; then
  PRIVATE_RTB="$(aws ec2 create-route-table --region "${REGION}" --vpc-id "${VPC_ID}" --query RouteTable.RouteTableId --output text)"
  tag_name "${PRIVATE_RTB}" "${PREFIX}-private-rtb"
fi
aws ec2 create-route --region "${REGION}" --route-table-id "${PRIVATE_RTB}" \
  --destination-cidr-block 0.0.0.0/0 --nat-gateway-id "${NAT_ID}" >/dev/null 2>&1 \
  || aws ec2 replace-route --region "${REGION}" --route-table-id "${PRIVATE_RTB}" \
       --destination-cidr-block 0.0.0.0/0 --nat-gateway-id "${NAT_ID}" >/dev/null

# Private subnets, one per CIDR, spread over the public subnets' AZs.
PUBLIC_AZS=($(aws ec2 describe-subnets --region "${REGION}" --subnet-ids "${SUBNET_ARR[@]}" \
  --query 'Subnets[].AvailabilityZone' --output text))
IFS=',' read -r -a PRIVATE_CIDR_ARR <<<"${PRIVATE_SUBNET_CIDRS}"
for i in "${!PRIVATE_CIDR_ARR[@]}"; do
  cidr="${PRIVATE_CIDR_ARR[$i]}"
  az="${PUBLIC_AZS[$((i % ${#PUBLIC_AZS[@]}))]}"
  sid="$(aws ec2 describe-subnets --region "${REGION}" --filters Name=vpc-id,Values="${VPC_ID}" Name=cidr-block,Values="${cidr}" \
    --query 'Subnets[0].SubnetId' --output text)"
  if [[ -z "${sid}" || "${sid}" == "None" ]]; then
    sid="$(aws ec2 create-subnet --region "${REGION}" --vpc-id "${VPC_ID}" --cidr-block "${cidr}" \
      --availability-zone "${az}" --query Subnet.SubnetId --output text)"
    tag_name "${sid}" "${PREFIX}-private-$((i + 1))"
    echo "created private subnet ${sid} (${cidr}, ${az})"
  fi
  aws ec2 associate-route-table --region "${REGION}" --route-table-id "${PRIVATE_RTB}" --subnet-id "${sid}" >/dev/null 2>&1 || true
  PRIVATE_SUBNETS+=("${sid}")
done
fi  # end of create-private-network branch

RUNTIME_SG_ID="$(ensure_sg "${RUNTIME_SG_NAME}" "${PREFIX} AgentCore Runtime ENIs (egress only)" "${RUNTIME_VPC_ID}")"
MOUNT_SG_ID="$(ensure_sg "${MOUNT_SG_NAME}" "${PREFIX} S3 Files mount targets: NFS from runtime" "${RUNTIME_VPC_ID}")"
aws ec2 authorize-security-group-ingress --region "${REGION}" --group-id "${MOUNT_SG_ID}" \
  --ip-permissions "IpProtocol=tcp,FromPort=2049,ToPort=2049,UserIdGroupPairs=[{GroupId=${RUNTIME_SG_ID},Description=nfs-from-runtime}]" \
  >/dev/null 2>&1 || true

# ------------------------------------------------- 4b. S3 bucket + S3 Files
aws s3api head-bucket --bucket "${WORKSPACE_BUCKET}" --region "${REGION}" >/dev/null 2>&1 || {
  if [[ "${REGION}" == "us-east-1" ]]; then
    aws s3api create-bucket --bucket "${WORKSPACE_BUCKET}" --region "${REGION}" >/dev/null
  else
    aws s3api create-bucket --bucket "${WORKSPACE_BUCKET}" --region "${REGION}" \
      --create-bucket-configuration LocationConstraint="${REGION}" >/dev/null
  fi
  aws s3api put-public-access-block --bucket "${WORKSPACE_BUCKET}" --region "${REGION}" \
    --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
  echo "created bucket ${WORKSPACE_BUCKET}"
}
BUCKET_ARN="arn:aws:s3:::${WORKSPACE_BUCKET}"
# S3 Files requires versioning on the backing bucket.
if [[ "$(aws s3api get-bucket-versioning --bucket "${WORKSPACE_BUCKET}" --region "${REGION}" --query Status --output text)" != "Enabled" ]]; then
  aws s3api put-bucket-versioning --bucket "${WORKSPACE_BUCKET}" --region "${REGION}" --versioning-configuration Status=Enabled
  echo "enabled versioning on ${WORKSPACE_BUCKET}"
fi

# Service role S3 Files assumes to read/write the bucket (principal is
# elasticfilesystem.amazonaws.com, as created by the console).
TRUST_S3FILES="$(jq -cn --arg acct "${ACCOUNT_ID}" --arg arn "arn:aws:s3files:${REGION}:${ACCOUNT_ID}:file-system/*" \
  '{Version:"2012-10-17",Statement:[{Effect:"Allow",Principal:{Service:"elasticfilesystem.amazonaws.com"},Action:"sts:AssumeRole",
    Condition:{StringEquals:{"aws:SourceAccount":$acct},ArnLike:{"aws:SourceArn":$arn}}}]}')"
ensure_role "${S3FILES_ROLE_NAME}" "${TRUST_S3FILES}"
aws iam put-role-policy --role-name "${S3FILES_ROLE_NAME}" --policy-name BucketAccess --policy-document "$(jq -cn --arg b "${BUCKET_ARN}" '{
  Version:"2012-10-17",Statement:[
    {Effect:"Allow",Action:["s3:ListBucket*"],Resource:$b},
    {Effect:"Allow",Action:["s3:AbortMultipartUpload","s3:DeleteObject*","s3:GetObject*","s3:List*","s3:PutObject*"],Resource:($b+"/*")},
    {Effect:"Allow",Action:["events:DeleteRule","events:DisableRule","events:EnableRule","events:PutRule","events:PutTargets","events:RemoveTargets"],
     Resource:"arn:aws:events:*:*:rule/DO-NOT-DELETE-S3-Files*",Condition:{StringEquals:{"events:ManagedBy":"elasticfilesystem.amazonaws.com"}}},
    {Effect:"Allow",Action:["events:DescribeRule","events:ListRuleNamesByTarget","events:ListRules","events:ListTargetsByRule"],Resource:"arn:aws:events:*:*:rule/*"}]}')"
S3FILES_ROLE_ARN="$(role_arn "${S3FILES_ROLE_NAME}")"

FS_ID="$(aws s3files list-file-systems --region "${REGION}" --output json \
  | jq -r --arg b "${BUCKET_ARN}" '.fileSystems[] | select(.bucket==$b) | .fileSystemId' | head -1)"
if [[ -z "${FS_ID}" ]]; then
  FS_ID=""
  for _ in 1 2 3 4 5 6; do
    FS_ID="$(aws s3files create-file-system --region "${REGION}" --bucket "${BUCKET_ARN}" \
      --role-arn "${S3FILES_ROLE_ARN}" --accept-bucket-warning \
      --tags key=project,value="${PREFIX}" --query fileSystemId --output text 2>/tmp/${PREFIX}-s3files.err || true)"
    [[ -n "${FS_ID}" && "${FS_ID}" != "None" ]] && break
    echo "  create-file-system failed ($(head -c 200 /tmp/${PREFIX}-s3files.err)); retrying in 10s"; FS_ID=""; sleep 10
  done
  [[ -n "${FS_ID}" ]] || die "could not create S3 Files file system"
  echo "created S3 Files file system ${FS_ID}"
fi
FS_ARN="arn:aws:s3files:${REGION}:${ACCOUNT_ID}:file-system/${FS_ID}"
for _ in $(seq 1 60); do
  fs_state="$(aws s3files get-file-system --region "${REGION}" --file-system-id "${FS_ID}" --query status --output text)"
  [[ "${fs_state}" == "available" ]] && break
  echo "  file system ${FS_ID}: ${fs_state}"
  sleep 10
done
for sid in "${PRIVATE_SUBNETS[@]}"; do
  az_id="$(aws ec2 describe-subnets --region "${REGION}" --subnet-ids "${sid}" --query 'Subnets[0].AvailabilityZoneId' --output text)"
  existing="$(aws s3files list-mount-targets --region "${REGION}" --file-system-id "${FS_ID}" --output json \
    | jq -r --arg az "${az_id}" '.mountTargets[] | select(.availabilityZoneId==$az) | .mountTargetId' | head -1)"
  if [[ -z "${existing}" ]]; then
    aws s3files create-mount-target --region "${REGION}" --file-system-id "${FS_ID}" --subnet-id "${sid}" \
      --security-groups "${MOUNT_SG_ID}" >/dev/null
    echo "created mount target in ${sid} (${az_id})"
  fi
done
AP_ARN="$(aws s3files list-access-points --region "${REGION}" --file-system-id "${FS_ID}" --output json \
  | jq -r --arg root "${S3FILES_ROOT}" '.accessPoints[] | select(.rootDirectory.path==$root) | .accessPointArn' | head -1)"
if [[ -z "${AP_ARN}" ]]; then
  # The agent container runs as root inside the microVM, so the access point is 0:0.
  AP_ARN="$(aws s3files create-access-point --region "${REGION}" --file-system-id "${FS_ID}" \
    --posix-user uid=0,gid=0 \
    --root-directory "path=${S3FILES_ROOT},creationPermissions={ownerUid=0,ownerGid=0,permissions=700}" \
    --query accessPointArn --output text)"
  echo "created access point ${AP_ARN}"
fi
aws iam put-role-policy --role-name "${RUNTIME_ROLE_NAME}" --policy-name S3FilesMount --policy-document "$(jq -cn --arg fs "${FS_ARN}" --arg ap "${AP_ARN}" '{
  Version:"2012-10-17",Statement:[
    {Sid:"Mount",Effect:"Allow",Action:["s3files:ClientMount","s3files:ClientWrite","s3files:ClientRootAccess"],
     Resource:$fs,Condition:{ArnEquals:{"s3files:AccessPointArn":$ap}}},
    {Sid:"DescribeAccessPoint",Effect:"Allow",Action:["s3files:GetAccessPoint","s3files:GetFileSystem","s3files:ListMountTargets"],
     Resource:[$fs,$ap]}]}')"
# Wait until the file system and every mount target are usable before creating the Runtime.
for _ in $(seq 1 60); do
  fs_state="$(aws s3files get-file-system --region "${REGION}" --file-system-id "${FS_ID}" --query status --output text)"
  states="$(aws s3files list-mount-targets --region "${REGION}" --file-system-id "${FS_ID}" --query 'mountTargets[].status' --output text)"
  echo "  file system: ${fs_state}; mount targets: ${states:-<none>}"
  if [[ "${fs_state}" == "available" && -n "${states}" ]] && ! grep -qv 'available' <<<"$(tr '\t' '\n' <<<"${states}")"; then break; fi
  sleep 10
done

# ------------------------------------------------------- 4c. AgentCore Runtime
echo "Runtime ${RUNTIME_NAME}: VPC mode, S3 Files ${FS_ID} at ${MOUNT_PATH}"
ENV_JSON="$(python3 - "${MODEL_ID}" "${REGION}" "${MAX_PARALLEL_AGENTS}" "${MAX_TURNS}" "${USERS_ROOT}" "${MOUNT_PATH}" <<'PY'
import json, sys
model, region, parallel, turns, users_root, mount = sys.argv[1:]
print(json.dumps({
    "ANTHROPIC_MODEL": model,
    "ANTHROPIC_SMALL_FAST_MODEL": model,
    "AWS_REGION": region,
    "AWS_DEFAULT_REGION": region,
    "MAX_PARALLEL_AGENTS": parallel,
    "MAX_TURNS": turns,
    "USERS_ROOT": users_root,
    "WORKSPACE_MOUNT": mount,
}, separators=(",", ":")))
PY
)"
ARTIFACT_JSON="$(jq -cn --arg uri "${RUNTIME_IMAGE_URI}" '{containerConfiguration:{containerUri:$uri}}')"
FS_JSON="$(jq -cn --arg mp "${MOUNT_PATH}" --arg ap "${AP_ARN}" '[{s3FilesAccessPoint:{accessPointArn:$ap,mountPath:$mp}}]')"
PRIVATE_SUBNETS_JSON="$(printf '%s\n' "${PRIVATE_SUBNETS[@]}" | jq -R . | jq -sc .)"
RUNTIME_NETWORK_JSON="$(jq -cn --argjson subnets "${PRIVATE_SUBNETS_JSON}" --arg sg "${RUNTIME_SG_ID}" \
  '{networkMode:"VPC",networkModeConfig:{subnets:$subnets,securityGroups:[$sg]}}')"
LIFECYCLE_JSON="$(jq -cn --argjson idle "${IDLE_TIMEOUT_S}" --argjson max "${MAX_LIFETIME_S}" \
  '{idleRuntimeSessionTimeout:$idle,maxLifetime:$max}')"

EXISTING_ID="$(aws bedrock-agentcore-control list-agent-runtimes --region "${REGION}" \
  --query "agentRuntimes[?agentRuntimeName=='${RUNTIME_NAME}'].agentRuntimeId | [0]" \
  --output text | grep -v '^None$' | head -1 || true)"
create_runtime() {
  aws bedrock-agentcore-control create-agent-runtime --region "${REGION}" \
    --agent-runtime-name "${RUNTIME_NAME}" --role-arn "${RUNTIME_ROLE_ARN}" \
    --agent-runtime-artifact "${ARTIFACT_JSON}" \
    --network-configuration "${RUNTIME_NETWORK_JSON}" \
    --protocol-configuration '{"serverProtocol":"HTTP"}' \
    --environment-variables "${ENV_JSON}" \
    --filesystem-configurations "${FS_JSON}" \
    --lifecycle-configuration "${LIFECYCLE_JSON}" \
    --query agentRuntimeId --output text
}
if [[ -z "${EXISTING_ID}" ]]; then
  # IAM eventual consistency: a freshly created role may not be assumable yet.
  RUNTIME_ID=""
  for _ in 1 2 3 4 5 6; do
    RUNTIME_ID="$(create_runtime 2>/tmp/${PREFIX}-create.err || true)"
    [[ -n "${RUNTIME_ID}" ]] && break
    echo "  create-agent-runtime failed ($(head -c 200 /tmp/${PREFIX}-create.err)); retrying in 10s"
    sleep 10
  done
  [[ -n "${RUNTIME_ID}" ]] || die "could not create Runtime"
  echo "created Runtime ${RUNTIME_ID}"
else
  RUNTIME_ID="${EXISTING_ID}"
  if [[ "${SKIP_RUNTIME_UPDATE:-0}" == "1" ]]; then
    echo "keeping Runtime ${RUNTIME_ID} as is (SKIP_RUNTIME_UPDATE=1)"
  else
    update_runtime() {
      aws bedrock-agentcore-control update-agent-runtime --region "${REGION}" \
        --agent-runtime-id "${RUNTIME_ID}" --role-arn "${RUNTIME_ROLE_ARN}" \
        --agent-runtime-artifact "${ARTIFACT_JSON}" \
        --network-configuration "${RUNTIME_NETWORK_JSON}" \
        --protocol-configuration '{"serverProtocol":"HTTP"}' \
        --environment-variables "${ENV_JSON}" \
        --filesystem-configurations "${FS_JSON}" \
        --lifecycle-configuration "${LIFECYCLE_JSON}" >/dev/null
    }
    # IAM propagation of the S3FilesMount policy may lag by a few seconds.
    retry 6 10 update_runtime || die "could not update Runtime"
    echo "updated Runtime ${RUNTIME_ID} (new version)"
    RUNTIME_VERSION_BUMPED=1
  fi
fi
STATUS=""
for _ in $(seq 1 120); do
  STATUS="$(aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id "${RUNTIME_ID}" \
    --region "${REGION}" --query status --output text)"
  echo "  runtime status=${STATUS}"
  [[ "${STATUS}" == "READY" ]] && break
  [[ "${STATUS}" == *FAILED* ]] && die "Runtime deployment failed"
  sleep 5
done
[[ "${STATUS}" == "READY" ]] || die "timed out waiting for Runtime READY"
RUNTIME_ARN="$(aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id "${RUNTIME_ID}" \
  --region "${REGION}" --query agentRuntimeArn --output text)"

# A new Runtime version replaces every session's execution environment, so the
# pool records (generations, ACTIVE flags) are stale. User data is on S3 Files
# and unaffected. Wipe the pool; the reconciler re-warms it within a minute.
if [[ "${RUNTIME_VERSION_BUMPED:-0}" == "1" ]]; then
  echo "resetting pool table ${TABLE_NAME} after Runtime version bump"
  uv run python - "${TABLE_NAME}" "${REGION}" <<'PY'
import boto3, sys
table, region = sys.argv[1:]
client = boto3.client("dynamodb", region_name=region)
kwargs = {"TableName": table, "ProjectionExpression": "PK, SK"}
keys = []
while True:
    page = client.scan(**kwargs)
    keys.extend(page.get("Items", []))
    if "LastEvaluatedKey" not in page:
        break
    kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
for i in range(0, len(keys), 25):
    client.batch_write_item(RequestItems={table: [{"DeleteRequest": {"Key": k}} for k in keys[i:i + 25]]})
print(f"deleted {len(keys)} pool items")
PY
fi

# Data-plane + DynamoDB policy shared by the router task and the reconciler.
POOL_ACCESS_POLICY="$(python3 - "${TABLE_ARN}" "${RUNTIME_ARN}" <<'PY'
import json, sys
table_arn, runtime_arn = sys.argv[1:]
print(json.dumps({"Version": "2012-10-17", "Statement": [
    {"Sid": "PoolTable", "Effect": "Allow",
     "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem",
                "dynamodb:Query", "dynamodb:TransactWriteItems", "dynamodb:ConditionCheckItem"],
     "Resource": [table_arn, f"{table_arn}/index/*"]},
    {"Sid": "RuntimeDataPlane", "Effect": "Allow",
     # InvokeAgentRuntimeForUser is required because every call carries runtimeUserId.
     "Action": ["bedrock-agentcore:InvokeAgentRuntime", "bedrock-agentcore:InvokeAgentRuntimeForUser",
                "bedrock-agentcore:StopRuntimeSession"],
     "Resource": [runtime_arn, f"{runtime_arn}/runtime-endpoint/*"]},
    {"Sid": "Metrics", "Effect": "Allow", "Action": "cloudwatch:PutMetricData", "Resource": "*"},
]}))
PY
)"
aws iam put-role-policy --role-name "${ROUTER_TASK_ROLE_NAME}" --policy-name PoolAccess --policy-document "${POOL_ACCESS_POLICY}"
aws iam put-role-policy --role-name "${LAMBDA_ROLE_NAME}" --policy-name PoolAccess --policy-document "${POOL_ACCESS_POLICY}"

# ---------------------------------------------------------- 5. network + ALB
log "[5/8] security groups + internal ALB (ingress only from ${CLIENT_CIDR})"
ALB_SG_ID="$(ensure_sg "${ALB_SG_NAME}" "${PREFIX} internal ALB: client CIDR only")"
TASK_SG_ID="$(ensure_sg "${TASK_SG_NAME}" "${PREFIX} router tasks: ALB only")"
# Deliberately never 0.0.0.0/0: only the operator's client CIDR reaches the ALB.
aws ec2 authorize-security-group-ingress --region "${REGION}" --group-id "${ALB_SG_ID}" \
  --ip-permissions "IpProtocol=tcp,FromPort=80,ToPort=80,IpRanges=[{CidrIp=${CLIENT_CIDR},Description=demo-client}]" \
  >/dev/null 2>&1 || true
aws ec2 authorize-security-group-ingress --region "${REGION}" --group-id "${TASK_SG_ID}" \
  --ip-permissions "IpProtocol=tcp,FromPort=8080,ToPort=8080,UserIdGroupPairs=[{GroupId=${ALB_SG_ID},Description=from-alb}]" \
  >/dev/null 2>&1 || true

ALB_ARN="$(aws elbv2 describe-load-balancers --region "${REGION}" --names "${ALB_NAME}" \
  --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null || true)"
if [[ -z "${ALB_ARN}" || "${ALB_ARN}" == "None" ]]; then
  ALB_ARN="$(aws elbv2 create-load-balancer --region "${REGION}" --name "${ALB_NAME}" \
    --scheme internal --type application --ip-address-type ipv4 \
    --subnets "${SUBNET_ARR[@]}" --security-groups "${ALB_SG_ID}" \
    --tags Key=project,Value="${PREFIX}" \
    --query 'LoadBalancers[0].LoadBalancerArn' --output text)"
  echo "created ALB"
fi
aws elbv2 modify-load-balancer-attributes --region "${REGION}" --load-balancer-arn "${ALB_ARN}" \
  --attributes Key=idle_timeout.timeout_seconds,Value="${ALB_IDLE_TIMEOUT_S}" >/dev/null
ALB_DNS="$(aws elbv2 describe-load-balancers --region "${REGION}" --load-balancer-arns "${ALB_ARN}" \
  --query 'LoadBalancers[0].DNSName' --output text)"

TG_ARN="$(aws elbv2 describe-target-groups --region "${REGION}" --names "${TG_NAME}" \
  --query 'TargetGroups[0].TargetGroupArn' --output text 2>/dev/null || true)"
if [[ -z "${TG_ARN}" || "${TG_ARN}" == "None" ]]; then
  TG_ARN="$(aws elbv2 create-target-group --region "${REGION}" --name "${TG_NAME}" \
    --protocol HTTP --port 8080 --vpc-id "${VPC_ID}" --target-type ip \
    --health-check-path /healthz --health-check-interval-seconds 15 --healthy-threshold-count 2 \
    --query 'TargetGroups[0].TargetGroupArn' --output text)"
  aws elbv2 modify-target-group-attributes --region "${REGION}" --target-group-arn "${TG_ARN}" \
    --attributes Key=deregistration_delay.timeout_seconds,Value=30 >/dev/null
fi
LISTENER_ARN="$(aws elbv2 describe-listeners --region "${REGION}" --load-balancer-arn "${ALB_ARN}" \
  --query 'Listeners[?Port==`80`].ListenerArn | [0]' --output text 2>/dev/null || true)"
if [[ -z "${LISTENER_ARN}" || "${LISTENER_ARN}" == "None" ]]; then
  aws elbv2 create-listener --region "${REGION}" --load-balancer-arn "${ALB_ARN}" \
    --protocol HTTP --port 80 --default-actions Type=forward,TargetGroupArn="${TG_ARN}" >/dev/null
fi

# ------------------------------------------------------------- 6. ECS router
log "[6/8] ECS Fargate router service"
aws logs describe-log-groups --region "${REGION}" --log-group-name-prefix "${LOG_GROUP}" \
  --query "logGroups[?logGroupName=='${LOG_GROUP}'].logGroupName" --output text | grep -q . \
  || { aws logs create-log-group --region "${REGION}" --log-group-name "${LOG_GROUP}";
       aws logs put-retention-policy --region "${REGION}" --log-group-name "${LOG_GROUP}" --retention-in-days 7; }
aws ecs describe-clusters --region "${REGION}" --clusters "${CLUSTER_NAME}" \
  --query "clusters[?status=='ACTIVE'].clusterName" --output text | grep -q . \
  || aws ecs create-cluster --region "${REGION}" --cluster-name "${CLUSTER_NAME}" \
       --tags key=project,value="${PREFIX}" >/dev/null

TASK_DEF_JSON="$(python3 - <<PY
import json
env = {
    "AWS_REGION": "${REGION}",
    "POOL_TABLE": "${TABLE_NAME}",
    "RUNTIME_ARN": "${RUNTIME_ARN}",
    "MODEL_ID": "${MODEL_ID}",
    "APP_VERSION": "${APP_VERSION}",
    "MAX_INFLIGHT": "${MAX_INFLIGHT}",
    "TARGET_INFLIGHT": "${TARGET_INFLIGHT}",
    "MAX_SESSIONS": "${MAX_SESSIONS}",
    "MIN_WARM_SESSIONS": "${MIN_WARM_SESSIONS}",
    "AFFINITY_TTL_S": "${AFFINITY_TTL_S}",
    "QUEUE_WAIT_S": "${QUEUE_WAIT_S}",
    "IDLE_STOP_S": "${IDLE_STOP_S}",
    "SESSION_ID_PREFIX": "${PREFIX}",
    "CONTEXT_EXTERNALIZED": "${CONTEXT_EXTERNALIZED}",
}
print(json.dumps({
    "family": "${TASK_FAMILY}",
    "networkMode": "awsvpc",
    "requiresCompatibilities": ["FARGATE"],
    "cpu": "${ROUTER_CPU}",
    "memory": "${ROUTER_MEMORY}",
    "runtimePlatform": {"cpuArchitecture": "ARM64", "operatingSystemFamily": "LINUX"},
    "executionRoleArn": "${ECS_EXEC_ROLE_ARN}",
    "taskRoleArn": "${ROUTER_TASK_ROLE_ARN}",
    "containerDefinitions": [{
        "name": "router",
        "image": "${ROUTER_IMAGE_URI}",
        "essential": True,
        "portMappings": [{"containerPort": 8080, "protocol": "tcp"}],
        "environment": [{"name": k, "value": v} for k, v in env.items()],
        "logConfiguration": {"logDriver": "awslogs", "options": {
            "awslogs-group": "${LOG_GROUP}", "awslogs-region": "${REGION}", "awslogs-stream-prefix": "router"}},
        "healthCheck": {"command": ["CMD-SHELL", "python3 -c \"import urllib.request;urllib.request.urlopen('http://localhost:8080/healthz')\" || exit 1"],
                        "interval": 30, "timeout": 5, "retries": 3, "startPeriod": 20},
    }],
}))
PY
)"
TASK_DEF_ARN="$(aws ecs register-task-definition --region "${REGION}" --cli-input-json "${TASK_DEF_JSON}" \
  --query taskDefinition.taskDefinitionArn --output text)"
SUBNETS_JSON="$(printf '%s\n' "${SUBNET_ARR[@]}" | jq -R . | jq -sc .)"
NETWORK_JSON="$(jq -cn --argjson subnets "${SUBNETS_JSON}" --arg sg "${TASK_SG_ID}" \
  '{awsvpcConfiguration:{subnets:$subnets,securityGroups:[$sg],assignPublicIp:"ENABLED"}}')"
SERVICE_STATUS="$(aws ecs describe-services --region "${REGION}" --cluster "${CLUSTER_NAME}" --services "${SERVICE_NAME}" \
  --query 'services[0].status' --output text 2>/dev/null || true)"
if [[ "${SERVICE_STATUS}" == "ACTIVE" ]]; then
  aws ecs update-service --region "${REGION}" --cluster "${CLUSTER_NAME}" --service "${SERVICE_NAME}" \
    --task-definition "${TASK_DEF_ARN}" --desired-count "${ROUTER_DESIRED_COUNT}" \
    --network-configuration "${NETWORK_JSON}" --force-new-deployment >/dev/null
  echo "updated service"
else
  aws ecs create-service --region "${REGION}" --cluster "${CLUSTER_NAME}" --service-name "${SERVICE_NAME}" \
    --task-definition "${TASK_DEF_ARN}" --desired-count "${ROUTER_DESIRED_COUNT}" --launch-type FARGATE \
    --network-configuration "${NETWORK_JSON}" \
    --load-balancers "targetGroupArn=${TG_ARN},containerName=router,containerPort=8080" \
    --health-check-grace-period-seconds 60 --tags key=project,value="${PREFIX}" >/dev/null
  echo "created service"
fi

# --------------------------------------------------------- 7. reconciler
log "[7/8] reconciler Lambda + EventBridge ${RECONCILE_RATE}"
rm -rf "${BUILD_DIR}/reconciler" && mkdir -p "${BUILD_DIR}/reconciler"
cp router/config.py router/store.py router/invoker.py reconciler/handler.py "${BUILD_DIR}/reconciler/"
# Vendor boto3 so the bundled Lambda SDK version does not matter for bedrock-agentcore.
uv pip install --quiet --python 3.13 --target "${BUILD_DIR}/reconciler" \
  "boto3==$(uv run python -c 'import boto3; print(boto3.__version__)')" >/dev/null
( cd "${BUILD_DIR}/reconciler" && rm -f ../reconciler.zip && zip -qr ../reconciler.zip . -x '*.pyc' -x '__pycache__/*' )
LAMBDA_ENV="$(jq -cn \
  --arg table "${TABLE_NAME}" --arg arn "${RUNTIME_ARN}" --arg model "${MODEL_ID}" --arg ver "${APP_VERSION}" \
  --arg mi "${MAX_INFLIGHT}" --arg ti "${TARGET_INFLIGHT}" --arg ms "${MAX_SESSIONS}" --arg mw "${MIN_WARM_SESSIONS}" \
  --arg idle "${IDLE_STOP_S}" --arg prefix "${PREFIX}" \
  '{Variables:{POOL_TABLE:$table,RUNTIME_ARN:$arn,MODEL_ID:$model,APP_VERSION:$ver,MAX_INFLIGHT:$mi,TARGET_INFLIGHT:$ti,MAX_SESSIONS:$ms,MIN_WARM_SESSIONS:$mw,IDLE_STOP_S:$idle,SESSION_ID_PREFIX:$prefix}}')"
if aws lambda get-function --region "${REGION}" --function-name "${LAMBDA_NAME}" >/dev/null 2>&1; then
  aws lambda update-function-code --region "${REGION}" --function-name "${LAMBDA_NAME}" \
    --zip-file "fileb://${BUILD_DIR}/reconciler.zip" >/dev/null
  aws lambda wait function-updated --region "${REGION}" --function-name "${LAMBDA_NAME}"
  aws lambda update-function-configuration --region "${REGION}" --function-name "${LAMBDA_NAME}" \
    --environment "${LAMBDA_ENV}" --timeout 300 --memory-size 512 >/dev/null
  echo "updated Lambda"
else
  retry 6 10 aws lambda create-function --region "${REGION}" --function-name "${LAMBDA_NAME}" \
    --runtime python3.13 --architectures arm64 --handler handler.handler \
    --role "${LAMBDA_ROLE_ARN}" --zip-file "fileb://${BUILD_DIR}/reconciler.zip" \
    --timeout 300 --memory-size 512 --environment "${LAMBDA_ENV}" \
    --tags project="${PREFIX}" >/dev/null
  echo "created Lambda"
fi
aws lambda wait function-updated --region "${REGION}" --function-name "${LAMBDA_NAME}"
# One reconciler at a time: overlapping runs would fight over inflight repair.
aws lambda put-function-concurrency --region "${REGION}" --function-name "${LAMBDA_NAME}" \
  --reserved-concurrent-executions 1 >/dev/null
LAMBDA_ARN="$(aws lambda get-function --region "${REGION}" --function-name "${LAMBDA_NAME}" \
  --query Configuration.FunctionArn --output text)"
RULE_ARN="$(aws events put-rule --region "${REGION}" --name "${RULE_NAME}" \
  --schedule-expression "${RECONCILE_RATE}" --state ENABLED --query RuleArn --output text)"
aws lambda add-permission --region "${REGION}" --function-name "${LAMBDA_NAME}" \
  --statement-id "${RULE_NAME}" --action lambda:InvokeFunction \
  --principal events.amazonaws.com --source-arn "${RULE_ARN}" >/dev/null 2>&1 || true
aws events put-targets --region "${REGION}" --rule "${RULE_NAME}" \
  --targets "Id=reconciler,Arn=${LAMBDA_ARN}" >/dev/null

# ------------------------------------------------------------- 8. pool.json
log "[8/8] waiting for the router service to stabilise"
aws ecs wait services-stable --region "${REGION}" --cluster "${CLUSTER_NAME}" --services "${SERVICE_NAME}"
python3 - "${POOL_CONFIG}" <<PY
import json, sys
json.dump({
    "region": "${REGION}",
    "prefix": "${PREFIX}",
    "accountId": "${ACCOUNT_ID}",
    "tableName": "${TABLE_NAME}",
    "runtimeName": "${RUNTIME_NAME}",
    "runtimeId": "${RUNTIME_ID}",
    "runtimeArn": "${RUNTIME_ARN}",
    "runtimeImageUri": "${RUNTIME_IMAGE_URI}",
    "routerImageUri": "${ROUTER_IMAGE_URI}",
    "modelId": "${MODEL_ID}",
    "mountPath": "${MOUNT_PATH}",
    "usersRoot": "${USERS_ROOT}",
    "workspaceBucket": "${WORKSPACE_BUCKET}",
    "s3FilesFileSystemId": "${FS_ID}",
    "s3FilesAccessPointArn": "${AP_ARN}",
    "s3FilesRoot": "${S3FILES_ROOT}",
    "runtimeSubnetIds": ${PRIVATE_SUBNETS_JSON},
    "runtimeVpcId": "${RUNTIME_VPC_ID}",
    "runtimeSecurityGroupId": "${RUNTIME_SG_ID}",
    "natGatewayId": "${NAT_ID}",
    "routerUrl": "http://${ALB_DNS}",
    "albArn": "${ALB_ARN}",
    "albDns": "${ALB_DNS}",
    "targetGroupArn": "${TG_ARN}",
    "albSecurityGroupId": "${ALB_SG_ID}",
    "taskSecurityGroupId": "${TASK_SG_ID}",
    "clientCidr": "${CLIENT_CIDR}",
    "vpcId": "${VPC_ID}",
    "subnetIds": "${SUBNET_IDS}".split(","),
    "clusterName": "${CLUSTER_NAME}",
    "serviceName": "${SERVICE_NAME}",
    "taskDefinitionArn": "${TASK_DEF_ARN}",
    "logGroup": "${LOG_GROUP}",
    "lambdaName": "${LAMBDA_NAME}",
    "ruleName": "${RULE_NAME}",
    "roles": {
        "runtime": "${RUNTIME_ROLE_NAME}",
        "s3files": "${S3FILES_ROLE_NAME}",
        "ecsExec": "${ECS_EXEC_ROLE_NAME}",
        "routerTask": "${ROUTER_TASK_ROLE_NAME}",
        "lambda": "${LAMBDA_ROLE_NAME}",
    },
    "scheduler": {
        "maxInflight": ${MAX_INFLIGHT}, "targetInflight": ${TARGET_INFLIGHT},
        "maxSessions": ${MAX_SESSIONS}, "minWarmSessions": ${MIN_WARM_SESSIONS},
        "affinityTtlS": ${AFFINITY_TTL_S}, "queueWaitS": ${QUEUE_WAIT_S}, "idleStopS": ${IDLE_STOP_S},
    },
}, open(sys.argv[1], "w", encoding="utf-8"), indent=2)
open(sys.argv[1], "a").write("\n")
PY
echo "wrote ${POOL_CONFIG}"
echo "router: http://${ALB_DNS}   (reachable only from ${CLIENT_CIDR})"
for _ in $(seq 1 20); do
  if curl -s -m 5 "http://${ALB_DNS}/healthz" | grep -q '"ok"'; then echo "router healthy"; break; fi
  sleep 6
done
