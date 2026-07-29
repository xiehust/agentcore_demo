#!/usr/bin/env bash
# Phase 2: the interceptor Lambda (inbound JWT validation) and the tool Lambda
# (outbound token exchange + RDS read). Both attached to the isolated VPC.
source "$(dirname "$0")/lib.sh"

: "${IDP_JWKS_URL:?run 01-idp.sh first}"
BUILD="$ROOT_DIR/build"
INT_FN="$PREFIX-interceptor"
TOOL_FN="$PREFIX-tool"

# ---------- shared execution role ----------
ROLE_NAME="$PREFIX-lambda-role"
if ! aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  log "Creating Lambda execution role"
  aws iam create-role --role-name "$ROLE_NAME" --assume-role-policy-document '{
    "Version":"2012-10-17",
    "Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},
                  "Action":"sts:AssumeRole"}]}' >/dev/null
  aws iam attach-role-policy --role-name "$ROLE_NAME" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole >/dev/null
  sleep 12
fi
LAMBDA_ROLE_ARN=$(aws iam get-role --role-name "$ROLE_NAME" --query Role.Arn --output text)
save LAMBDA_ROLE_ARN "$LAMBDA_ROLE_ARN"
ok "role $LAMBDA_ROLE_ARN"

# ---------- packages ----------
# x86_64 wheels to match the default Lambda architecture.
# This build host is arm64 while Lambda here is x86_64, so the install must be
# cross-platform: --platform requires --only-binary=:all: and --target.
build_pkg() { # dir_name source_file requirement...
  local name="$1" src="$2"; shift 2
  local dir="$BUILD/$name"
  rm -rf "$dir"; mkdir -p "$dir"
  python3 -m pip install --quiet --target "$dir" \
    --platform manylinux2014_x86_64 --python-version 3.12 \
    --implementation cp --only-binary=:all: "$@" >/dev/null
  cp "$src" "$dir/"
  (cd "$dir" && zip -qr "$BUILD/$name.zip" .)
  ok "$name.zip $(du -h "$BUILD/$name.zip" | cut -f1)"
}
log "Building deployment packages"
build_pkg interceptor "$ROOT_DIR/lambda/interceptor.py" "pyjwt[crypto]==2.10.1" "cryptography==46.0.3"
build_pkg tool        "$ROOT_DIR/lambda/tool.py"        "pymysql==1.1.1"

# ---------- deploy ----------
deploy_fn() { # name handler zip env_json
  local name="$1" handler="$2" zip="$3" envjson="$4"
  if aws lambda get-function --function-name "$name" --region "$REGION" >/dev/null 2>&1; then
    aws lambda update-function-code --function-name "$name" \
      --zip-file "fileb://$zip" --region "$REGION" >/dev/null
    aws lambda wait function-updated --function-name "$name" --region "$REGION"
    aws lambda update-function-configuration --function-name "$name" \
      --environment "$envjson" --region "$REGION" >/dev/null
  else
    aws lambda create-function --function-name "$name" \
      --runtime python3.12 --architectures x86_64 --handler "$handler" \
      --role "$LAMBDA_ROLE_ARN" --zip-file "fileb://$zip" \
      --timeout 30 --memory-size 512 \
      --vpc-config "SubnetIds=$SUBNET_PRIV_A,$SUBNET_PRIV_B,SecurityGroupIds=$SG_LAMBDA" \
      --environment "$envjson" --region "$REGION" >/dev/null
  fi
  aws lambda wait function-updated --function-name "$name" --region "$REGION"
}

log "Deploying interceptor Lambda"
deploy_fn "$INT_FN" interceptor.lambda_handler "$BUILD/interceptor.zip" \
  "Variables={IDP_JWKS_URL=$IDP_JWKS_URL,IDP_ISSUER=$IDP_ISSUER,IDP_AUDIENCE=$IDP_AUDIENCE,REQUIRED_SCOPE=$REQUIRED_SCOPE}"
INT_ARN=$(aws lambda get-function --function-name "$INT_FN" --region "$REGION" \
  --query 'Configuration.FunctionArn' --output text)
save INTERCEPTOR_ARN "$INT_ARN"
save INTERCEPTOR_FN "$INT_FN"
ok "interceptor $INT_ARN"

log "Deploying tool Lambda"
deploy_fn "$TOOL_FN" tool.lambda_handler "$BUILD/tool.zip" \
  "Variables={IDP_TOKEN_URL=$IDP_TOKEN_URL,IDP_ISSUER=$IDP_ISSUER,IDP_CLIENT_ID=$IDP_CLIENT_ID,IDP_CLIENT_SECRET=$IDP_CLIENT_SECRET,DB_HOST=$DB_HOST,DB_USER=$DB_USER,DB_PASS=$DB_PASS,DB_NAME=$DB_NAME}"
TOOL_ARN=$(aws lambda get-function --function-name "$TOOL_FN" --region "$REGION" \
  --query 'Configuration.FunctionArn' --output text)
save TOOL_ARN "$TOOL_ARN"
save TOOL_FN "$TOOL_FN"
ok "tool $TOOL_ARN"

# ---------- confirm the IdP is up and privately reachable ----------
log "Checking the IdP from inside the VPC (via the tool Lambda)"
for attempt in $(seq 20); do
  aws lambda invoke --function-name "$TOOL_FN" --region "$REGION" \
    --payload '{}' --cli-binary-format raw-in-base64-out \
    --client-context "$(printf '{"custom":{"bedrockAgentCoreToolName":"idp_reachability"}}' | base64 -w0)" \
    "$BUILD/idp-check.json" >/dev/null 2>&1 || true
  if grep -q discovery_issuer "$BUILD/idp-check.json" 2>/dev/null; then
    python3 -m json.tool "$BUILD/idp-check.json"
    ok "IdP reachable privately"
    break
  fi
  [[ $attempt -eq 20 ]] && { warn "IdP not reachable yet:"; cat "$BUILD/idp-check.json"; exit 1; }
  sleep 15
done

log "Phase 2 complete."
