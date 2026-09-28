#!/usr/bin/env bash
# Phase 1b: the S3 bootstrap bucket and the SSM-only EC2 instance profile.
# 04-apigw-vpclink.sh creates the same two things (idempotently); this script
# exists so projects that only need the isolated VPC + RDS + bucket (e.g.
# 13-private-idp-workaround) do not have to deploy the whole API Gateway path.
source "$(dirname "$0")/lib.sh"

: "${VPC_ID:?run 01-vpc-rds.sh first}"

BUCKET="$PREFIX-$ACCOUNT_ID-$REGION"
if ! aws s3api head-bucket --bucket "$BUCKET" --region "$REGION" >/dev/null 2>&1; then
  log "Creating bootstrap bucket $BUCKET"
  aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
    --create-bucket-configuration "LocationConstraint=$REGION" >/dev/null
fi
save BUCKET "$BUCKET"
ok "bucket $BUCKET"

EC2_ROLE="$PREFIX-ec2-role"
if ! aws iam get-role --role-name "$EC2_ROLE" >/dev/null 2>&1; then
  log "Creating EC2 role + instance profile (SSM only, for debugging)"
  aws iam create-role --role-name "$EC2_ROLE" --assume-role-policy-document '{
    "Version":"2012-10-17",
    "Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},
                  "Action":"sts:AssumeRole"}]}' >/dev/null
  aws iam attach-role-policy --role-name "$EC2_ROLE" \
    --policy-arn "arn:$PARTITION:iam::aws:policy/AmazonSSMManagedInstanceCore" >/dev/null
  aws iam create-instance-profile --instance-profile-name "$EC2_ROLE" >/dev/null
  aws iam add-role-to-instance-profile --instance-profile-name "$EC2_ROLE" \
    --role-name "$EC2_ROLE" >/dev/null
  sleep 15
fi
ok "instance profile $EC2_ROLE"

log "Phase 1b complete."
