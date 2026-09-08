#!/usr/bin/env bash
# Tear down everything infra/deploy.sh created (reverse order). ECR images are
# kept unless DELETE_ECR_IMAGES=1; the S3 workspace bucket (user data) is kept
# unless DELETE_BUCKET=1. Set RUNTIME_SUBNET_IDS as at deploy time when
# external private subnets were reused.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/env.sh"
DELETE_ECR_IMAGES="${DELETE_ECR_IMAGES:-0}"

quiet() { "$@" >/dev/null 2>&1 || true; }

log "reconciler"
quiet aws events remove-targets --region "${REGION}" --rule "${RULE_NAME}" --ids reconciler
quiet aws events delete-rule --region "${REGION}" --name "${RULE_NAME}"
quiet aws lambda delete-function --region "${REGION}" --function-name "${LAMBDA_NAME}"

log "ECS service"
if aws ecs describe-services --region "${REGION}" --cluster "${CLUSTER_NAME}" --services "${SERVICE_NAME}" \
     --query 'services[0].status' --output text 2>/dev/null | grep -q ACTIVE; then
  aws ecs update-service --region "${REGION}" --cluster "${CLUSTER_NAME}" --service "${SERVICE_NAME}" --desired-count 0 >/dev/null
  aws ecs delete-service --region "${REGION}" --cluster "${CLUSTER_NAME}" --service "${SERVICE_NAME}" --force >/dev/null
  aws ecs wait services-inactive --region "${REGION}" --cluster "${CLUSTER_NAME}" --services "${SERVICE_NAME}" || true
fi
for arn in $(aws ecs list-task-definitions --region "${REGION}" --family-prefix "${TASK_FAMILY}" --query 'taskDefinitionArns[]' --output text 2>/dev/null); do
  quiet aws ecs deregister-task-definition --region "${REGION}" --task-definition "${arn}"
done
quiet aws ecs delete-cluster --region "${REGION}" --cluster "${CLUSTER_NAME}"
quiet aws logs delete-log-group --region "${REGION}" --log-group-name "${LOG_GROUP}"

log "ALB"
ALB_ARN="$(aws elbv2 describe-load-balancers --region "${REGION}" --names "${ALB_NAME}" \
  --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null || true)"
if [[ -n "${ALB_ARN}" && "${ALB_ARN}" != "None" ]]; then
  for l in $(aws elbv2 describe-listeners --region "${REGION}" --load-balancer-arn "${ALB_ARN}" --query 'Listeners[].ListenerArn' --output text); do
    quiet aws elbv2 delete-listener --region "${REGION}" --listener-arn "${l}"
  done
  aws elbv2 delete-load-balancer --region "${REGION}" --load-balancer-arn "${ALB_ARN}"
  aws elbv2 wait load-balancers-deleted --region "${REGION}" --load-balancer-arns "${ALB_ARN}" || true
fi
TG_ARN="$(aws elbv2 describe-target-groups --region "${REGION}" --names "${TG_NAME}" \
  --query 'TargetGroups[0].TargetGroupArn' --output text 2>/dev/null || true)"
[[ -n "${TG_ARN}" && "${TG_ARN}" != "None" ]] && quiet aws elbv2 delete-target-group --region "${REGION}" --target-group-arn "${TG_ARN}"

log "AgentCore Runtime"
RUNTIME_ID="$(aws bedrock-agentcore-control list-agent-runtimes --region "${REGION}" \
  --query "agentRuntimes[?agentRuntimeName=='${RUNTIME_NAME}'].agentRuntimeId | [0]" --output text | grep -v '^None$' || true)"
if [[ -n "${RUNTIME_ID}" ]]; then
  aws bedrock-agentcore-control delete-agent-runtime --region "${REGION}" --agent-runtime-id "${RUNTIME_ID}" >/dev/null
  echo "deleted Runtime ${RUNTIME_ID}"
fi

log "DynamoDB"
quiet aws dynamodb delete-table --region "${REGION}" --table-name "${TABLE_NAME}"

log "S3 Files (access points, mount targets, file system)"
BUCKET_ARN="arn:aws:s3:::${WORKSPACE_BUCKET}"
FS_ID="$(aws s3files list-file-systems --region "${REGION}" --output json 2>/dev/null \
  | jq -r --arg b "${BUCKET_ARN}" '.fileSystems[] | select(.bucket==$b) | .fileSystemId' | head -1)"
if [[ -n "${FS_ID}" ]]; then
  for ap in $(aws s3files list-access-points --region "${REGION}" --file-system-id "${FS_ID}" --query 'accessPoints[].accessPointId' --output text); do
    quiet aws s3files delete-access-point --region "${REGION}" --access-point-id "${ap}"
  done
  for mt in $(aws s3files list-mount-targets --region "${REGION}" --file-system-id "${FS_ID}" --query 'mountTargets[].mountTargetId' --output text); do
    quiet aws s3files delete-mount-target --region "${REGION}" --mount-target-id "${mt}"
  done
  for _ in $(seq 1 30); do
    left="$(aws s3files list-mount-targets --region "${REGION}" --file-system-id "${FS_ID}" --query 'length(mountTargets)' --output text 2>/dev/null || echo 0)"
    [[ "${left}" == "0" ]] && break
    sleep 10
  done
  quiet aws s3files delete-file-system --region "${REGION}" --file-system-id "${FS_ID}"
  echo "deleted S3 Files file system ${FS_ID}"
fi
if [[ "${DELETE_BUCKET:-0}" == "1" ]]; then
  quiet aws s3 rb "s3://${WORKSPACE_BUCKET}" --force --region "${REGION}"
  echo "deleted bucket ${WORKSPACE_BUCKET}"
else
  echo "bucket ${WORKSPACE_BUCKET} retained (DELETE_BUCKET=1 to remove user workspaces)"
fi

log "security groups (waiting for ENIs to detach)"
if [[ -z "${VPC_ID}" ]]; then
  VPC_ID="$(aws ec2 describe-vpcs --region "${REGION}" --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text)"
fi
sg_id() {  # name [vpc]
  aws ec2 describe-security-groups --region "${REGION}" --filters Name=vpc-id,Values="${2:-${VPC_ID}}" Name=group-name,Values="$1" \
    --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null | grep -v '^None$' || true
}
RUNTIME_VPC_ID="${VPC_ID}"
if [[ -n "${RUNTIME_SUBNET_IDS}" ]]; then
  RUNTIME_VPC_ID="$(aws ec2 describe-subnets --region "${REGION}" --subnet-ids "${RUNTIME_SUBNET_IDS%%,*}" --query 'Subnets[0].VpcId' --output text 2>/dev/null || echo "${VPC_ID}")"
fi
TASK_SG_ID="$(sg_id "${TASK_SG_NAME}")"
ALB_SG_ID="$(sg_id "${ALB_SG_NAME}")"
RUNTIME_SG_ID="$(sg_id "${RUNTIME_SG_NAME}" "${RUNTIME_VPC_ID}")"
MOUNT_SG_ID="$(sg_id "${MOUNT_SG_NAME}" "${RUNTIME_VPC_ID}")"
# Cross-references must go before the groups themselves.
[[ -n "${MOUNT_SG_ID}" && -n "${RUNTIME_SG_ID}" ]] && quiet aws ec2 revoke-security-group-ingress --region "${REGION}" --group-id "${MOUNT_SG_ID}" \
  --ip-permissions "IpProtocol=tcp,FromPort=2049,ToPort=2049,UserIdGroupPairs=[{GroupId=${RUNTIME_SG_ID}}]"
[[ -n "${TASK_SG_ID}" && -n "${ALB_SG_ID}" ]] && quiet aws ec2 revoke-security-group-ingress --region "${REGION}" --group-id "${TASK_SG_ID}" \
  --ip-permissions "IpProtocol=tcp,FromPort=8080,ToPort=8080,UserIdGroupPairs=[{GroupId=${ALB_SG_ID}}]"
for _ in $(seq 1 40); do
  ok=1
  for var in TASK_SG_ID ALB_SG_ID MOUNT_SG_ID RUNTIME_SG_ID; do
    id="${!var}"
    [[ -n "${id}" ]] || continue
    if aws ec2 delete-security-group --region "${REGION}" --group-id "${id}" >/dev/null 2>&1; then
      printf -v "${var}" ''
    else
      ok=0
    fi
  done
  [[ "${ok}" == 1 ]] && break
  sleep 10
done

log "private subnets, route table, NAT gateway, EIP"
for cidr in ${PRIVATE_SUBNET_CIDRS//,/ }; do
  sid="$(aws ec2 describe-subnets --region "${REGION}" --filters Name=vpc-id,Values="${VPC_ID}" Name=cidr-block,Values="${cidr}" \
    --query 'Subnets[0].SubnetId' --output text 2>/dev/null | grep -v '^None$' || true)"
  [[ -n "${sid}" ]] || continue
  for _ in $(seq 1 30); do
    aws ec2 delete-subnet --region "${REGION}" --subnet-id "${sid}" >/dev/null 2>&1 && { echo "deleted subnet ${sid}"; break; }
    sleep 10
  done
done
RTB="$(aws ec2 describe-route-tables --region "${REGION}" --filters Name=vpc-id,Values="${VPC_ID}" Name=tag:Name,Values="${PREFIX}-private-rtb" \
  --query 'RouteTables[0].RouteTableId' --output text 2>/dev/null | grep -v '^None$' || true)"
[[ -n "${RTB}" ]] && quiet aws ec2 delete-route-table --region "${REGION}" --route-table-id "${RTB}"
NAT_ID="$(aws ec2 describe-nat-gateways --region "${REGION}" --filter Name=vpc-id,Values="${VPC_ID}" Name=tag:Name,Values="${PREFIX}-nat" \
  Name=state,Values=pending,available --query 'NatGateways[0].NatGatewayId' --output text 2>/dev/null | grep -v '^None$' || true)"
if [[ -n "${NAT_ID}" ]]; then
  aws ec2 delete-nat-gateway --region "${REGION}" --nat-gateway-id "${NAT_ID}" >/dev/null
  aws ec2 wait nat-gateway-deleted --region "${REGION}" --nat-gateway-ids "${NAT_ID}" || true
  echo "deleted NAT gateway ${NAT_ID}"
fi
EIP_ALLOC="$(aws ec2 describe-addresses --region "${REGION}" --filters Name=tag:Name,Values="${PREFIX}-nat-eip" \
  --query 'Addresses[0].AllocationId' --output text 2>/dev/null | grep -v '^None$' || true)"
[[ -n "${EIP_ALLOC}" ]] && quiet aws ec2 release-address --region "${REGION}" --allocation-id "${EIP_ALLOC}"

log "IAM roles"
delete_role() {
  local name="$1"
  aws iam get-role --role-name "${name}" >/dev/null 2>&1 || return 0
  for p in $(aws iam list-role-policies --role-name "${name}" --query 'PolicyNames[]' --output text); do
    aws iam delete-role-policy --role-name "${name}" --policy-name "${p}"
  done
  for p in $(aws iam list-attached-role-policies --role-name "${name}" --query 'AttachedPolicies[].PolicyArn' --output text); do
    aws iam detach-role-policy --role-name "${name}" --policy-arn "${p}"
  done
  aws iam delete-role --role-name "${name}"
  echo "deleted role ${name}"
}
delete_role "${RUNTIME_ROLE_NAME}"
delete_role "${S3FILES_ROLE_NAME}"
delete_role "${ECS_EXEC_ROLE_NAME}"
delete_role "${ROUTER_TASK_ROLE_NAME}"
delete_role "${LAMBDA_ROLE_NAME}"

if [[ "${DELETE_ECR_IMAGES}" == "1" ]]; then
  log "ECR images"
  quiet aws ecr batch-delete-image --region "${REGION}" --repository-name "${REPO}" \
    --image-ids imageTag="${RUNTIME_TAG}" imageTag="${ROUTER_TAG}"
else
  echo "ECR images retained (DELETE_ECR_IMAGES=1 to remove)"
fi
rm -f "${POOL_CONFIG}"
echo "destroy complete"
