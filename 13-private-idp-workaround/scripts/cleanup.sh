#!/usr/bin/env bash
# Remove everything this project created. The VPC / RDS / bucket belong to
# 11-vpc-no-egress-workaround and are left alone.
# Usage: cleanup.sh --yes
source "$(dirname "$0")/lib.sh"

if [[ "${1:-}" != "--yes" ]]; then
  echo "Deletes the '$PREFIX' gateway, target, interceptor + tool Lambdas,"
  echo "the IdP instance, its security group and IAM roles in $REGION."
  echo "The VPC / RDS from 11-vpc-no-egress-workaround are NOT touched."
  echo "Re-run with --yes to proceed."
  exit 1
fi
try() { "$@" >/dev/null 2>&1 || true; }

# ---------- gateway ----------
if [[ -n "${IDP_GW_ID:-}" ]]; then
  for tid in $(aws bedrock-agentcore-control list-gateway-targets \
        --gateway-identifier "$IDP_GW_ID" --region "$REGION" \
        --query 'items[].targetId' --output text 2>/dev/null); do
    log "Deleting target $tid"
    try aws bedrock-agentcore-control delete-gateway-target \
      --gateway-identifier "$IDP_GW_ID" --target-id "$tid" --region "$REGION"
  done
  sleep 5
  log "Deleting gateway $IDP_GW_ID"
  try aws bedrock-agentcore-control delete-gateway \
    --gateway-identifier "$IDP_GW_ID" --region "$REGION"
fi

# ---------- lambdas ----------
for fn in "${INTERCEPTOR_FN:-}" "${TOOL_FN:-}"; do
  [[ -z "$fn" ]] && continue
  log "Deleting Lambda $fn"
  try aws lambda delete-function --function-name "$fn" --region "$REGION"
done

# ---------- IdP instance ----------
if [[ -n "${IDP_INSTANCE_ID:-}" ]]; then
  log "Terminating IdP instance $IDP_INSTANCE_ID"
  try aws ec2 terminate-instances --instance-ids "$IDP_INSTANCE_ID" --region "$REGION"
  try aws ec2 wait instance-terminated --instance-ids "$IDP_INSTANCE_ID" --region "$REGION"
fi

# ---------- security group (needs Lambda ENIs to drain first) ----------
if [[ -n "${SG_IDP:-}" ]]; then
  log "Waiting for ENIs to release, then deleting $SG_IDP"
  for _ in $(seq 40); do
    PERMS=$(aws ec2 describe-security-groups --group-ids "$SG_IDP" --region "$REGION" \
      --query 'SecurityGroups[0].IpPermissions' --output json 2>/dev/null || echo '[]')
    [[ "$PERMS" != "[]" ]] && try aws ec2 revoke-security-group-ingress \
      --group-id "$SG_IDP" --ip-permissions "$PERMS" --region "$REGION"
    if aws ec2 delete-security-group --group-id "$SG_IDP" --region "$REGION" >/dev/null 2>&1; then
      ok "sg deleted"; break
    fi
    sleep 15
  done
fi

# ---------- S3 payload ----------
[[ -n "${BUCKET:-}" ]] && try aws s3 rm "s3://$BUCKET/idp.tar.gz" --region "$REGION"

# ---------- IAM ----------
log "Deleting IAM roles"
try aws iam delete-role-policy --role-name "$PREFIX-gw-role" --policy-name invoke-lambdas
try aws iam delete-role --role-name "$PREFIX-gw-role"
try aws iam detach-role-policy --role-name "$PREFIX-lambda-role" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole
try aws iam delete-role --role-name "$PREFIX-lambda-role"

rm -rf "$ROOT_DIR/build"
mv "$STATE_FILE" "$STATE_FILE.deleted" 2>/dev/null || true
log "Cleanup complete. Signing keys were under build/keys and are now gone."
