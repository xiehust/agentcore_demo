#!/usr/bin/env bash
# Phase 2: the VPC-attached Lambda that reaches the private RDS instance,
# plus a direct invoke to seed the demo schema (no bastion needed).
source "$(dirname "$0")/lib.sh"

: "${DB_HOST:?run 01-vpc-rds.sh first}"
FN_NAME="$PREFIX-db-tool"
BUILD="$ROOT_DIR/build/lambda"

# ---------- package ----------
log "Building deployment package (pymysql is pure Python, no compilation)"
rm -rf "$BUILD"; mkdir -p "$BUILD"
python3 -m pip install --quiet --target "$BUILD" "pymysql==1.1.1"
cp "$ROOT_DIR/lambda/handler.py" "$BUILD/"
(cd "$BUILD" && zip -qr "$ROOT_DIR/build/lambda.zip" .)
ok "package $(du -h "$ROOT_DIR/build/lambda.zip" | cut -f1)"

# ---------- execution role ----------
ROLE_NAME="$PREFIX-lambda-role"
if ! aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  log "Creating Lambda execution role"
  aws iam create-role --role-name "$ROLE_NAME" --assume-role-policy-document '{
    "Version":"2012-10-17",
    "Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},
                  "Action":"sts:AssumeRole"}]}' >/dev/null
  # VPCAccessExecutionRole covers CloudWatch Logs *and* the ENI create/delete
  # permissions a VPC-attached Lambda needs.
  aws iam attach-role-policy --role-name "$ROLE_NAME" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole >/dev/null
  sleep 12  # let the role propagate before Lambda validates it
fi
LAMBDA_ROLE_ARN=$(aws iam get-role --role-name "$ROLE_NAME" --query Role.Arn --output text)
save LAMBDA_ROLE_ARN "$LAMBDA_ROLE_ARN"
ok "role $LAMBDA_ROLE_ARN"

# ---------- function ----------
ENV_JSON="Variables={DB_HOST=$DB_HOST,DB_USER=$DB_USER,DB_PASS=$DB_PASS,DB_NAME=$DB_NAME}"
if aws lambda get-function --function-name "$FN_NAME" --region "$REGION" >/dev/null 2>&1; then
  log "Updating existing function"
  aws lambda update-function-code --function-name "$FN_NAME" \
    --zip-file "fileb://$ROOT_DIR/build/lambda.zip" --region "$REGION" >/dev/null
  aws lambda wait function-updated --function-name "$FN_NAME" --region "$REGION"
  aws lambda update-function-configuration --function-name "$FN_NAME" \
    --environment "$ENV_JSON" --region "$REGION" >/dev/null
else
  log "Creating VPC-attached Lambda function"
  aws lambda create-function --function-name "$FN_NAME" \
    --runtime python3.12 --handler handler.lambda_handler \
    --role "$LAMBDA_ROLE_ARN" \
    --zip-file "fileb://$ROOT_DIR/build/lambda.zip" \
    --timeout 30 --memory-size 512 \
    --vpc-config "SubnetIds=$SUBNET_PRIV_A,$SUBNET_PRIV_B,SecurityGroupIds=$SG_LAMBDA" \
    --environment "$ENV_JSON" \
    --region "$REGION" >/dev/null
fi
aws lambda wait function-updated --function-name "$FN_NAME" --region "$REGION"
LAMBDA_ARN=$(aws lambda get-function --function-name "$FN_NAME" --region "$REGION" \
  --query 'Configuration.FunctionArn' --output text)
save LAMBDA_ARN "$LAMBDA_ARN"
save FN_NAME "$FN_NAME"
ok "lambda $LAMBDA_ARN"

# ---------- seed the database through the Lambda ----------
log "Seeding demo schema via direct Lambda invoke"
aws lambda invoke --function-name "$FN_NAME" --region "$REGION" \
  --payload '{"__admin":"init"}' --cli-binary-format raw-in-base64-out \
  "$ROOT_DIR/build/seed.json" >/dev/null
cat "$ROOT_DIR/build/seed.json"; echo
ok "schema seeded"

log "Phase 2 complete."
