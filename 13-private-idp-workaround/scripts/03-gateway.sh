#!/usr/bin/env bash
# Phase 3: AWS_IAM inbound in China (NONE elsewhere), followed by the private-JWT
# REQUEST interceptor, plus the tool Lambda as target.
source "$(dirname "$0")/lib.sh"

: "${INTERCEPTOR_ARN:?run 02-lambdas.sh first}"
: "${TOOL_ARN:?run 02-lambdas.sh first}"
GW_NAME="$PREFIX-gw"
ROLE_NAME="$PREFIX-gw-role"

# ---------- gateway service role ----------
if ! aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  log "Creating gateway service role"
  aws iam create-role --role-name "$ROLE_NAME" --assume-role-policy-document "{
    \"Version\":\"2012-10-17\",
    \"Statement\":[{
      \"Effect\":\"Allow\",
      \"Principal\":{\"Service\":\"bedrock-agentcore.amazonaws.com\"},
      \"Action\":\"sts:AssumeRole\",
      \"Condition\":{
        \"StringEquals\":{\"aws:SourceAccount\":\"$ACCOUNT_ID\"},
        \"ArnLike\":{\"aws:SourceArn\":\"arn:$PARTITION:bedrock-agentcore:$REGION:$ACCOUNT_ID:gateway/*\"}
      }
    }]}" >/dev/null
  sleep 12
fi
IDP_GW_ROLE_ARN=$(aws iam get-role --role-name "$ROLE_NAME" --query Role.Arn --output text)
save IDP_GW_ROLE_ARN "$IDP_GW_ROLE_ARN"

# Least privilege: only these two functions. The interceptor sits on the auth path,
# so a wildcard here would let anything be invoked as an authorizer.
aws iam put-role-policy --role-name "$ROLE_NAME" --policy-name invoke-lambdas \
  --policy-document "{
    \"Version\":\"2012-10-17\",
    \"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"lambda:InvokeFunction\",
      \"Resource\":[\"$INTERCEPTOR_ARN\",\"$TOOL_ARN\"]}]}" >/dev/null
ok "role $IDP_GW_ROLE_ARN"

# ---------- gateway ----------
GW_ID=$(aws bedrock-agentcore-control list-gateways --region "$REGION" \
  --query "items[?name=='$GW_NAME'].gatewayId | [0]" --output text)
if [[ "$GW_ID" == "None" || -z "$GW_ID" ]]; then
  log "Creating gateway (authorizerType=$INBOUND_AUTH + REQUEST interceptor)"
  cat > "$ROOT_DIR/build/interceptors.json" <<JSON
[
  {
    "interceptor": { "lambda": { "arn": "$INTERCEPTOR_ARN" } },
    "interceptionPoints": ["REQUEST"],
    "inputConfiguration": { "passRequestHeaders": true }
  }
]
JSON
  if aws bedrock-agentcore-control create-gateway help 2>/dev/null | col -b \
       | grep -q -- '--interceptor-configurations'; then
    GW_ID=$(aws bedrock-agentcore-control create-gateway \
      --name "$GW_NAME" \
      --role-arn "$IDP_GW_ROLE_ARN" \
      --protocol-type MCP \
      --authorizer-type "$INBOUND_AUTH" \
      --exception-level DEBUG \
      --interceptor-configurations "file://$ROOT_DIR/build/interceptors.json" \
      --description "Private IdP workaround: interceptor Lambda validates JWT" \
      --region "$REGION" --query gatewayId --output text)
  else
    # Older AWS CLI v2 builds lack --interceptor-configurations; boto3 has it.
    warn "aws CLI has no --interceptor-configurations; creating the gateway with boto3"
    GW_ID=$("$PY" - "$GW_NAME" "$IDP_GW_ROLE_ARN" "$ROOT_DIR/build/interceptors.json" "$INBOUND_AUTH" <<'PYEOF'
import json, sys, boto3
name, role, icfg, auth = sys.argv[1:5]
c = boto3.client("bedrock-agentcore-control")
r = c.create_gateway(
    name=name, roleArn=role, protocolType="MCP", authorizerType=auth,
    exceptionLevel="DEBUG",
    interceptorConfigurations=json.load(open(icfg)),
    description="Private IdP workaround: interceptor Lambda validates JWT")
print(r["gatewayId"])
PYEOF
)
  fi
fi
save IDP_GW_ID "$GW_ID"
# 04-verify.py reads these to know how to call the gateway.
save INBOUND_AUTH "$INBOUND_AUTH"
save TOKEN_HEADER "$TOKEN_HEADER"

log "Waiting for gateway READY"
for _ in $(seq 60); do
  read -r ST URL < <(aws bedrock-agentcore-control get-gateway --gateway-identifier "$GW_ID" \
    --region "$REGION" --query '[status,gatewayUrl]' --output text)
  [[ "$ST" == "READY" ]] && break
  [[ "$ST" == *FAILED* ]] && { aws bedrock-agentcore-control get-gateway \
      --gateway-identifier "$GW_ID" --region "$REGION" --query statusReasons; exit 1; }
  sleep 5
done
[[ "$ST" == "READY" ]] || { warn "Gateway did not become READY: $ST"; exit 1; }
save IDP_GW_URL "$URL"
save IDP_GW_ARN "arn:$PARTITION:bedrock-agentcore:$REGION:$ACCOUNT_ID:gateway/$GW_ID"
ok "gateway $GW_ID status=$ST"
ok "url $URL"

# ---------- target ----------
TGT_NAME="secureOrders"
TGT_ID=$(aws bedrock-agentcore-control list-gateway-targets --gateway-identifier "$GW_ID" \
  --region "$REGION" --query "items[?name=='$TGT_NAME'].targetId | [0]" --output text)
if [[ "$TGT_ID" == "None" || -z "$TGT_ID" ]]; then
  log "Creating Lambda target"
  cat > "$ROOT_DIR/build/target.json" <<JSON
{
  "mcp": {
    "lambda": {
      "lambdaArn": "$TOOL_ARN",
      "toolSchema": {
        "inlinePayload": [
          {
            "name": "secure_list_orders",
            "description": "Read orders from the private database. The tool exchanges client_credentials at the private IdP first.",
            "inputSchema": {
              "type": "object",
              "properties": {
                "status": {"type": "string", "description": "SHIPPED, PENDING or CANCELLED"},
                "limit": {"type": "integer", "description": "Max rows (1-100)"}
              },
              "required": []
            }
          },
          {
            "name": "idp_reachability",
            "description": "Prove the private IdP is reachable from inside the VPC and report its private IP.",
            "inputSchema": {"type": "object", "properties": {}, "required": []}
          }
        ]
      }
    }
  }
}
JSON
  TGT_ID=$(aws bedrock-agentcore-control create-gateway-target \
    --gateway-identifier "$GW_ID" --name "$TGT_NAME" \
    --description "Tool Lambda doing outbound token exchange with the private IdP" \
    --target-configuration "file://$ROOT_DIR/build/target.json" \
    --credential-provider-configurations '[{"credentialProviderType":"GATEWAY_IAM_ROLE"}]' \
    --region "$REGION" --query targetId --output text)
fi
save IDP_TGT_ID "$TGT_ID"

log "Waiting for target READY"
for _ in $(seq 60); do
  ST=$(aws bedrock-agentcore-control get-gateway-target --gateway-identifier "$GW_ID" \
    --target-id "$TGT_ID" --region "$REGION" --query status --output text)
  [[ "$ST" == "READY" ]] && break
  [[ "$ST" == *FAILED* || "$ST" == *UNSUCCESSFUL* ]] && {
    aws bedrock-agentcore-control get-gateway-target --gateway-identifier "$GW_ID" \
      --target-id "$TGT_ID" --region "$REGION" --query statusReasons; exit 1; }
  sleep 5
done
[[ "$ST" == "READY" ]] || { warn "Target did not become READY: $ST"; exit 1; }
ok "target $TGT_ID status=$ST"

# ---------- signed caller (AWS_IAM inbound only) ----------
# With AWS_IAM inbound every gateway request needs SigV4. The verifier sends its
# requests through this small Lambda, whose execution role does the signing. The
# test machine uses AWS_PROFILE to invoke Lambda, not to sign gateway requests.
# This is a test backend, not a public MCP proxy. It is NOT VPC-attached: it
# must reach the gateway's public endpoint, which the isolated VPC cannot.
if [[ "$INBOUND_AUTH" == "AWS_IAM" ]]; then
  CALLER_FN="$PREFIX-signed-caller"
  CALLER_ROLE="$PREFIX-caller-role"
  if ! aws iam get-role --role-name "$CALLER_ROLE" >/dev/null 2>&1; then
    log "Creating signed-caller role"
    aws iam create-role --role-name "$CALLER_ROLE" --assume-role-policy-document '{
      "Version":"2012-10-17",
      "Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},
                    "Action":"sts:AssumeRole"}]}' >/dev/null
    aws iam attach-role-policy --role-name "$CALLER_ROLE" \
      --policy-arn "arn:$PARTITION:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole" >/dev/null
    sleep 12
  fi
  # Least privilege: invoke this one gateway only.
  aws iam put-role-policy --role-name "$CALLER_ROLE" --policy-name invoke-gateway \
    --policy-document "{
      \"Version\":\"2012-10-17\",
      \"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"bedrock-agentcore:InvokeGateway\",
        \"Resource\":\"$IDP_GW_ARN\"}]}" >/dev/null
  CALLER_ROLE_ARN=$(aws iam get-role --role-name "$CALLER_ROLE" --query Role.Arn --output text)

  rm -f "$ROOT_DIR/build/signed_caller.zip"
  (cd "$ROOT_DIR/lambda" && python3 -m zipfile -c "$ROOT_DIR/build/signed_caller.zip" signed_caller.py)
  if aws lambda get-function --function-name "$CALLER_FN" --region "$REGION" >/dev/null 2>&1; then
    aws lambda update-function-code --function-name "$CALLER_FN" \
      --zip-file "fileb://$ROOT_DIR/build/signed_caller.zip" --region "$REGION" >/dev/null
  else
    log "Deploying signed-caller Lambda"
    aws lambda create-function --function-name "$CALLER_FN" \
      --runtime python3.12 --architectures x86_64 --handler signed_caller.lambda_handler \
      --role "$CALLER_ROLE_ARN" --zip-file "fileb://$ROOT_DIR/build/signed_caller.zip" \
      --timeout 90 --memory-size 256 --region "$REGION" >/dev/null
  fi
  aws lambda wait function-updated --function-name "$CALLER_FN" --region "$REGION"
  save CALLER_FN "$CALLER_FN"
  ok "signed caller $CALLER_FN (role may take ~10 s to be usable)"
fi

log "Phase 3 complete."
