#!/usr/bin/env bash
# Tear down everything created by this demo. Idempotent: safe to re-run.
# Usage: cleanup.sh --yes
source "$(dirname "$0")/lib.sh"

if [[ "${1:-}" != "--yes" ]]; then
  echo "This deletes the gateway, targets, API, NLB, EC2, Lambda, RDS, VPC and roles"
  echo "for prefix '$PREFIX' in $REGION."
  echo "Re-run with --yes to proceed."
  exit 1
fi

try() { "$@" >/dev/null 2>&1 || true; }

# ---------- AgentCore gateway ----------
if [[ -n "${GW_ID:-}" ]]; then
  log "Deleting gateway targets"
  for tid in $(aws bedrock-agentcore-control list-gateway-targets --gateway-identifier "$GW_ID" \
        --region "$REGION" --query 'items[].targetId' --output text 2>/dev/null); do
    try aws bedrock-agentcore-control delete-gateway-target \
      --gateway-identifier "$GW_ID" --target-id "$tid" --region "$REGION"
    ok "target $tid"
  done
  sleep 5
  log "Deleting gateway $GW_ID"
  try aws bedrock-agentcore-control delete-gateway --gateway-identifier "$GW_ID" --region "$REGION"
fi

# ---------- API Gateway ----------
[[ -n "${API_ID:-}" ]] && { log "Deleting REST API"; try aws apigateway delete-rest-api --rest-api-id "$API_ID" --region "$REGION"; }
if [[ -n "${VPC_LINK_ID:-}" ]]; then
  log "Deleting VPC Link (takes a few minutes)"
  try aws apigateway delete-vpc-link --vpc-link-id "$VPC_LINK_ID" --region "$REGION"
  for _ in $(seq 60); do
    aws apigateway get-vpc-link --vpc-link-id "$VPC_LINK_ID" --region "$REGION" >/dev/null 2>&1 || break
    sleep 10
  done
  ok "vpc link gone"
fi

# ---------- load balancer ----------
[[ -n "${NLB_LISTENER:-}" ]] && try aws elbv2 delete-listener --listener-arn "$NLB_LISTENER" --region "$REGION"
[[ -n "${NLB_ARN:-}" ]] && { log "Deleting NLB"; try aws elbv2 delete-load-balancer --load-balancer-arn "$NLB_ARN" --region "$REGION"; sleep 30; }
[[ -n "${TG_ARN:-}" ]] && try aws elbv2 delete-target-group --target-group-arn "$TG_ARN" --region "$REGION"

# ---------- EC2 ----------
if [[ -n "${APP_INSTANCE_ID:-}" ]]; then
  log "Terminating app instance"
  try aws ec2 terminate-instances --instance-ids "$APP_INSTANCE_ID" --region "$REGION"
  try aws ec2 wait instance-terminated --instance-ids "$APP_INSTANCE_ID" --region "$REGION"
  ok "instance terminated"
fi

# ---------- Lambda ----------
[[ -n "${FN_NAME:-}" ]] && { log "Deleting Lambda"; try aws lambda delete-function --function-name "$FN_NAME" --region "$REGION"; }

# ---------- RDS ----------
if [[ -n "${DB_ID:-}" ]]; then
  log "Deleting RDS instance (takes several minutes)"
  try aws rds delete-db-instance --db-instance-identifier "$DB_ID" \
    --skip-final-snapshot --delete-automated-backups --region "$REGION"
  try aws rds wait db-instance-deleted --db-instance-identifier "$DB_ID" --region "$REGION"
  ok "rds deleted"
fi
try aws rds delete-db-subnet-group --db-subnet-group-name "$PREFIX-subnets" --region "$REGION"

# ---------- S3 ----------
if [[ -n "${BUCKET:-}" ]]; then
  log "Emptying and deleting bucket"
  try aws s3 rm "s3://$BUCKET" --recursive --region "$REGION"
  try aws s3api delete-bucket --bucket "$BUCKET" --region "$REGION"
fi

# ---------- VPC endpoints ----------
EPS=$(aws ec2 describe-vpc-endpoints --region "$REGION" \
  --filters "Name=vpc-id,Values=${VPC_ID:-none}" --query 'VpcEndpoints[].VpcEndpointId' --output text 2>/dev/null)
if [[ -n "$EPS" ]]; then
  log "Deleting VPC endpoints"
  # shellcheck disable=SC2086
  try aws ec2 delete-vpc-endpoints --vpc-endpoint-ids $EPS --region "$REGION"
  sleep 30
fi

# ---------- Lambda ENIs must drain before SGs/subnets can go ----------
if [[ -n "${VPC_ID:-}" ]]; then
  log "Waiting for managed ENIs to be released"
  for _ in $(seq 60); do
    N=$(aws ec2 describe-network-interfaces --region "$REGION" \
      --filters "Name=vpc-id,Values=$VPC_ID" --query 'length(NetworkInterfaces)' --output text)
    [[ "$N" == "0" ]] && break
    # Detached leftovers can be deleted directly.
    for eni in $(aws ec2 describe-network-interfaces --region "$REGION" \
          --filters "Name=vpc-id,Values=$VPC_ID" "Name=status,Values=available" \
          --query 'NetworkInterfaces[].NetworkInterfaceId' --output text); do
      try aws ec2 delete-network-interface --network-interface-id "$eni" --region "$REGION"
    done
    sleep 15
  done
  ok "ENIs remaining: ${N:-unknown}"
fi

# ---------- security groups ----------
# Drop cross-referencing rules first, otherwise deletion is refused.
for sg in "${SG_RDS:-}" "${SG_APP:-}" "${SG_LAMBDA:-}" "${SG_VPCE:-}"; do
  [[ -z "$sg" ]] && continue
  PERMS=$(aws ec2 describe-security-groups --group-ids "$sg" --region "$REGION" \
    --query 'SecurityGroups[0].IpPermissions' --output json 2>/dev/null)
  [[ "$PERMS" != "[]" && -n "$PERMS" ]] && \
    try aws ec2 revoke-security-group-ingress --group-id "$sg" \
      --ip-permissions "$PERMS" --region "$REGION"
done
for sg in "${SG_RDS:-}" "${SG_APP:-}" "${SG_LAMBDA:-}" "${SG_VPCE:-}"; do
  [[ -z "$sg" ]] && continue
  try aws ec2 delete-security-group --group-id "$sg" --region "$REGION"
  ok "sg $sg"
done

# ---------- subnets / route table / VPC ----------
for sn in "${SUBNET_PRIV_A:-}" "${SUBNET_PRIV_B:-}"; do
  [[ -n "$sn" ]] && try aws ec2 delete-subnet --subnet-id "$sn" --region "$REGION"
done
[[ -n "${RT_PRIV:-}" ]] && try aws ec2 delete-route-table --route-table-id "$RT_PRIV" --region "$REGION"
[[ -n "${VPC_ID:-}" ]] && { try aws ec2 delete-vpc --vpc-id "$VPC_ID" --region "$REGION"; ok "vpc ${VPC_ID}"; }

# ---------- IAM ----------
log "Deleting IAM roles"
try aws iam delete-role-policy --role-name "$PREFIX-gw-role" --policy-name invoke-lambda-target
try aws iam delete-role-policy --role-name "$PREFIX-gw-role" --policy-name invoke-apigw-target
try aws iam delete-role --role-name "$PREFIX-gw-role"
try aws iam detach-role-policy --role-name "$PREFIX-lambda-role" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole
try aws iam delete-role --role-name "$PREFIX-lambda-role"
try aws iam remove-role-from-instance-profile --instance-profile-name "$PREFIX-ec2-role" --role-name "$PREFIX-ec2-role"
try aws iam delete-instance-profile --instance-profile-name "$PREFIX-ec2-role"
try aws iam detach-role-policy --role-name "$PREFIX-ec2-role" \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
try aws iam delete-role --role-name "$PREFIX-ec2-role"

mv "$STATE_FILE" "$STATE_FILE.deleted" 2>/dev/null || true
log "Cleanup complete. Verify with: aws ec2 describe-vpcs --region $REGION"
