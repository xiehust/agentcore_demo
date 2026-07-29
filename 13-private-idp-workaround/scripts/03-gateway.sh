#!/usr/bin/env bash
# Phase 3: a gateway whose inbound auth is NONE, with the interceptor Lambda as
# the actual authorizer, plus the tool Lambda as target.
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
        \"ArnLike\":{\"aws:SourceArn\":\"arn:aws:bedrock-agentcore:$REGION:$ACCOUNT_ID:gateway/*\"}
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
  log "Creating gateway (authorizerType=NONE + REQUEST interceptor)"
  cat > "$ROOT_DIR/build/interceptors.json" <<JSON
[
  {
    "interceptor": { "lambda": { "arn": "$INTERCEPTOR_ARN" } },
    "interceptionPoints": ["REQUEST"],
    "inputConfiguration": { "passRequestHeaders": true }
  }
]
JSON
  GW_ID=$(aws bedrock-agentcore-control create-gateway \
    --name "$GW_NAME" \
    --role-arn "$IDP_GW_ROLE_ARN" \
    --protocol-type MCP \
    --authorizer-type NONE \
    --exception-level DEBUG \
    --interceptor-configurations "file://$ROOT_DIR/build/interceptors.json" \
    --description "Private IdP workaround: interceptor Lambda validates JWT" \
    --region "$REGION" --query gatewayId --output text)
fi
save IDP_GW_ID "$GW_ID"

log "Waiting for gateway READY"
for _ in $(seq 60); do
  read -r ST URL < <(aws bedrock-agentcore-control get-gateway --gateway-identifier "$GW_ID" \
    --region "$REGION" --query '[status,gatewayUrl]' --output text)
  [[ "$ST" == "READY" ]] && break
  [[ "$ST" == *FAILED* ]] && { aws bedrock-agentcore-control get-gateway \
      --gateway-identifier "$GW_ID" --region "$REGION" --query statusReasons; exit 1; }
  sleep 5
done
save IDP_GW_URL "$URL"
save IDP_GW_ARN "arn:aws:bedrock-agentcore:$REGION:$ACCOUNT_ID:gateway/$GW_ID"
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
ok "target $TGT_ID status=$ST"

log "Phase 3 complete."
