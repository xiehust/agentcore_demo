#!/usr/bin/env bash
# Phase 4 (Workaround 2): in-VPC EC2 HTTP service -> internal NLB -> API Gateway
# VPC Link -> regional REST API locked down to the AgentCore service principal.
source "$(dirname "$0")/lib.sh"

: "${DB_HOST:?run 01-vpc-rds.sh first}"

# ---------- ship the app into the isolated subnet via S3 ----------
BUCKET="$PREFIX-$ACCOUNT_ID-$REGION"
if ! aws s3api head-bucket --bucket "$BUCKET" --region "$REGION" >/dev/null 2>&1; then
  log "Creating bootstrap bucket $BUCKET"
  aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
    --create-bucket-configuration "LocationConstraint=$REGION" >/dev/null
fi
save BUCKET "$BUCKET"

log "Building app bundle (app.py + pure-Python pymysql)"
BUILD="$ROOT_DIR/build/app"
rm -rf "$BUILD"; mkdir -p "$BUILD"
python3 -m pip install --quiet --target "$BUILD" "pymysql==1.1.1"
cp "$ROOT_DIR/app/app.py" "$BUILD/"
tar -czf "$ROOT_DIR/build/app.tar.gz" -C "$BUILD" .
aws s3 cp "$ROOT_DIR/build/app.tar.gz" "s3://$BUCKET/app.tar.gz" --region "$REGION" >/dev/null
# Presigned URL so the instance needs no CLI and no credentials to bootstrap.
APP_URL=$(aws s3 presign "s3://$BUCKET/app.tar.gz" --expires-in 604800 --region "$REGION")
ok "app bundle uploaded"

# ---------- EC2 instance profile (SSM only, for debugging) ----------
EC2_ROLE="$PREFIX-ec2-role"
if ! aws iam get-role --role-name "$EC2_ROLE" >/dev/null 2>&1; then
  log "Creating EC2 role + instance profile"
  aws iam create-role --role-name "$EC2_ROLE" --assume-role-policy-document '{
    "Version":"2012-10-17",
    "Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},
                  "Action":"sts:AssumeRole"}]}' >/dev/null
  aws iam attach-role-policy --role-name "$EC2_ROLE" \
    --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore >/dev/null
  aws iam create-instance-profile --instance-profile-name "$EC2_ROLE" >/dev/null
  aws iam add-role-to-instance-profile --instance-profile-name "$EC2_ROLE" \
    --role-name "$EC2_ROLE" >/dev/null
  sleep 15
fi
ok "instance profile $EC2_ROLE"

# ---------- EC2 instance ----------
if [[ -z "${APP_INSTANCE_ID:-}" ]]; then
  AMI=$(aws ssm get-parameter --region "$REGION" \
    --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-6.1-x86_64 \
    --query Parameter.Value --output text)
  log "Launching in-VPC app instance ($AMI)"
  cat > "$ROOT_DIR/build/userdata.sh" <<EOF
#!/bin/bash
set -xe
mkdir -p /opt/app
# Fetch over the S3 gateway endpoint with a presigned URL: no internet, no CLI.
python3 -c "import urllib.request;urllib.request.urlretrieve('$APP_URL','/tmp/app.tar.gz')"
tar -xzf /tmp/app.tar.gz -C /opt/app
cat > /etc/systemd/system/app.service <<UNIT
[Unit]
Description=In-VPC orders API
[Service]
Environment=DB_HOST=$DB_HOST
Environment=DB_USER=$DB_USER
Environment=DB_PASS=$DB_PASS
Environment=DB_NAME=$DB_NAME
Environment=PYTHONPATH=/opt/app
ExecStart=/usr/bin/python3 /opt/app/app.py
Restart=always
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now app.service
EOF
  APP_INSTANCE_ID=$(aws ec2 run-instances --region "$REGION" \
    --image-id "$AMI" --instance-type t3.micro \
    --subnet-id "$SUBNET_PRIV_A" --security-group-ids "$SG_APP" \
    --no-associate-public-ip-address \
    --iam-instance-profile "Name=$EC2_ROLE" \
    --user-data "file://$ROOT_DIR/build/userdata.sh" \
    --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$PREFIX-app}]" \
    --query 'Instances[0].InstanceId' --output text)
  save APP_INSTANCE_ID "$APP_INSTANCE_ID"
fi
log "Waiting for instance to run"
aws ec2 wait instance-running --instance-ids "$APP_INSTANCE_ID" --region "$REGION"
ok "instance $APP_INSTANCE_ID"

# ---------- internal NLB ----------
if [[ -z "${NLB_ARN:-}" ]]; then
  log "Creating internal NLB"
  NLB_ARN=$(aws elbv2 create-load-balancer --name "$PREFIX-nlb" --type network \
    --scheme internal --subnets "$SUBNET_PRIV_A" --region "$REGION" \
    --query 'LoadBalancers[0].LoadBalancerArn' --output text)
  save NLB_ARN "$NLB_ARN"
fi
if [[ -z "${TG_ARN:-}" ]]; then
  TG_ARN=$(aws elbv2 create-target-group --name "$PREFIX-tg" \
    --protocol TCP --port 8080 --vpc-id "$VPC_ID" --target-type instance \
    --health-check-protocol HTTP --health-check-path /health \
    --health-check-interval-seconds 10 --healthy-threshold-count 2 \
    --region "$REGION" --query 'TargetGroups[0].TargetGroupArn' --output text)
  aws elbv2 register-targets --target-group-arn "$TG_ARN" \
    --targets "Id=$APP_INSTANCE_ID" --region "$REGION"
  save TG_ARN "$TG_ARN"
fi
if [[ -z "${NLB_LISTENER:-}" ]]; then
  NLB_LISTENER=$(aws elbv2 create-listener --load-balancer-arn "$NLB_ARN" \
    --protocol TCP --port 80 --default-actions "Type=forward,TargetGroupArn=$TG_ARN" \
    --region "$REGION" --query 'Listeners[0].ListenerArn' --output text)
  save NLB_LISTENER "$NLB_LISTENER"
fi
NLB_DNS=$(aws elbv2 describe-load-balancers --load-balancer-arns "$NLB_ARN" \
  --region "$REGION" --query 'LoadBalancers[0].DNSName' --output text)
save NLB_DNS "$NLB_DNS"
ok "NLB $NLB_DNS"

log "Waiting for the target to pass health checks (app bootstrap takes ~1-2 min)"
for _ in $(seq 60); do
  H=$(aws elbv2 describe-target-health --target-group-arn "$TG_ARN" --region "$REGION" \
    --query 'TargetHealthDescriptions[0].TargetHealth.State' --output text)
  [[ "$H" == "healthy" ]] && break
  sleep 10
done
[[ "$H" == "healthy" ]] || { warn "target state=$H — check SSM session on $APP_INSTANCE_ID"; exit 1; }
ok "target healthy"

# ---------- VPC Link ----------
if [[ -z "${VPC_LINK_ID:-}" ]]; then
  log "Creating API Gateway VPC Link (this takes a few minutes)"
  VPC_LINK_ID=$(aws apigateway create-vpc-link --name "$PREFIX-vpclink" \
    --target-arns "$NLB_ARN" --region "$REGION" --query id --output text)
  save VPC_LINK_ID "$VPC_LINK_ID"
fi
for _ in $(seq 120); do
  VL_ST=$(aws apigateway get-vpc-link --vpc-link-id "$VPC_LINK_ID" --region "$REGION" \
    --query status --output text)
  [[ "$VL_ST" == "AVAILABLE" ]] && break
  [[ "$VL_ST" == "FAILED" ]] && { aws apigateway get-vpc-link --vpc-link-id "$VPC_LINK_ID" \
      --region "$REGION" --query statusMessage; exit 1; }
  sleep 15
done
ok "VPC Link $VPC_LINK_ID status=$VL_ST"

# ---------- regional REST API ----------
if [[ -z "${API_ID:-}" ]]; then
  log "Creating regional REST API"
  API_ID=$(aws apigateway create-rest-api --name "$PREFIX-api" \
    --description "Reaches private RDS via VPC Link" \
    --endpoint-configuration "types=REGIONAL" \
    --region "$REGION" --query id --output text)
  save API_ID "$API_ID"

  ROOT_RES=$(aws apigateway get-resources --rest-api-id "$API_ID" --region "$REGION" \
    --query 'items[?path==`/`].id' --output text)

  # Each route becomes one tool on the AgentCore Gateway target.
  add_route() { # path-part operation-name query-params...
    local part="$1" op="$2"; shift 2
    local res_id req_params="" int_params=""
    res_id=$(aws apigateway create-resource --rest-api-id "$API_ID" \
      --parent-id "$ROOT_RES" --path-part "$part" --region "$REGION" \
      --query id --output text)
    for q in "$@"; do
      req_params+="method.request.querystring.$q=false,"
      int_params+="integration.request.querystring.$q=method.request.querystring.$q,"
    done
    # IAM auth => the gateway signs with SigV4 using its execution role.
    aws apigateway put-method --rest-api-id "$API_ID" --resource-id "$res_id" \
      --http-method GET --authorization-type AWS_IAM \
      --operation-name "$op" \
      ${req_params:+--request-parameters "${req_params%,}"} \
      --region "$REGION" >/dev/null
    aws apigateway put-method-response --rest-api-id "$API_ID" --resource-id "$res_id" \
      --http-method GET --status-code 200 \
      --response-models '{"application/json":"Empty"}' --region "$REGION" >/dev/null
    aws apigateway put-integration --rest-api-id "$API_ID" --resource-id "$res_id" \
      --http-method GET --type HTTP --integration-http-method GET \
      --uri "http://$NLB_DNS/$part" \
      --connection-type VPC_LINK --connection-id "$VPC_LINK_ID" \
      ${int_params:+--request-parameters "${int_params%,}"} \
      --region "$REGION" >/dev/null
    aws apigateway put-integration-response --rest-api-id "$API_ID" --resource-id "$res_id" \
      --http-method GET --status-code 200 --region "$REGION" >/dev/null
    ok "route /$part -> $op"
  }
  add_route orders listOrders status limit
  add_route dbinfo getDbInfo
fi
ok "REST API $API_ID"

# CreateDeployment is aggressively rate limited (~1 per 5s per account), so
# every deploy goes through a backoff loop.
deploy_stage() {
  for attempt in $(seq 10); do
    if aws apigateway create-deployment --rest-api-id "$API_ID" --stage-name prod \
         --region "$REGION" >/dev/null 2>&1; then
      ok "deployed stage prod"
      return 0
    fi
    sleep $((attempt * 5))
  done
  warn "create-deployment kept throttling"
  return 1
}

log "Deploying stage prod"
deploy_stage
save API_STAGE prod

# ---------- lock the API to the AgentCore service principal ----------
: "${GW_ARN:?run 03-gateway.sh first}"
log "Applying resource policy scoped to this gateway only"
POLICY=$(cat <<JSON
{"Version":"2012-10-17","Statement":[{
  "Effect":"Allow",
  "Principal":{"Service":"bedrock-agentcore.amazonaws.com"},
  "Action":"execute-api:Invoke",
  "Resource":["arn:aws:execute-api:$REGION:$ACCOUNT_ID:$API_ID/prod/*/*"],
  "Condition":{"ArnEquals":{"aws:SourceArn":"$GW_ARN"}}
}]}
JSON
)
aws apigateway update-rest-api --rest-api-id "$API_ID" --region "$REGION" \
  --patch-operations "op=replace,path=/policy,value='$(echo "$POLICY" | tr -d '\n' | sed "s/'/\\\\'/g")'" >/dev/null
# A resource-policy change only takes effect after a redeploy.
deploy_stage
ok "resource policy applied + redeployed"

# ---------- let the gateway role invoke this API ----------
log "Granting execute-api:Invoke to the gateway role (scoped to this API)"
aws iam put-role-policy --role-name "$PREFIX-gw-role" --policy-name invoke-apigw-target \
  --policy-document "{
    \"Version\":\"2012-10-17\",
    \"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"execute-api:Invoke\",
      \"Resource\":\"arn:aws:execute-api:$REGION:$ACCOUNT_ID:$API_ID/prod/*/*\"}]}" >/dev/null
save API_ENDPOINT "https://$API_ID.execute-api.$REGION.amazonaws.com/prod"
ok "api endpoint $API_ENDPOINT"

log "Phase 4 complete."
