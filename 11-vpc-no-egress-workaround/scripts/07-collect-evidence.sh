#!/usr/bin/env bash
# Phase 7: re-run every check and capture the raw output as evidence.
source "$(dirname "$0")/lib.sh"
cd "$ROOT_DIR"
OUT="$ROOT_DIR/results/evidence.txt"
mkdir -p "$ROOT_DIR/results"

{
echo "AgentCore no-VPC-egress workaround verification"
echo "region=$REGION  account=$ACCOUNT_ID  date=$(date -u +%FT%TZ)"
echo "gateway=$GW_ID  url=$GW_URL"
echo
echo "############ Network isolation of the target VPC ############"
echo "--- internet gateways attached to $VPC_ID ---"
aws ec2 describe-internet-gateways --region "$REGION" \
  --filters "Name=attachment.vpc-id,Values=$VPC_ID" --query 'length(InternetGateways)'
echo "--- NAT gateways in $VPC_ID ---"
aws ec2 describe-nat-gateways --region "$REGION" \
  --filter "Name=vpc-id,Values=$VPC_ID" --query 'length(NatGateways)'
echo "--- routes in the only route table ---"
aws ec2 describe-route-tables --route-table-ids "$RT_PRIV" --region "$REGION" \
  --query 'RouteTables[0].Routes' --output json
echo "--- RDS public accessibility ---"
aws rds describe-db-instances --db-instance-identifier "$DB_ID" --region "$REGION" \
  --query 'DBInstances[0].{PubliclyAccessible:PubliclyAccessible,Endpoint:Endpoint.Address}' --output json
echo "--- TCP 3306 reachability from outside the VPC (expect timeout) ---"
timeout 15 python3 -c "
import socket
try:
    socket.create_connection(('$DB_HOST',3306),timeout=10); print('REACHABLE - unexpected')
except Exception as e: print('unreachable:', type(e).__name__)
"
echo
echo "############ Tools exposed by the gateway ############"
python3 mcp_client.py list
echo
echo "############ Workaround 1: Gateway -> Lambda(VPC) -> RDS ############"
echo "--- rdsLambda___db_info ---"
python3 mcp_client.py call rdsLambda___db_info
echo "--- rdsLambda___list_orders {\"status\":\"SHIPPED\"} ---"
python3 mcp_client.py call rdsLambda___list_orders '{"status":"SHIPPED"}'
echo
echo "############ Workaround 2: Gateway -> API GW -> VPC Link -> NLB -> EC2 -> RDS ############"
echo "--- rdsApi___getDbInfo ---"
python3 mcp_client.py call rdsApi___getDbInfo
echo "--- rdsApi___listOrders {\"status\":\"PENDING\"} ---"
python3 mcp_client.py call rdsApi___listOrders '{"status":"PENDING"}'
echo
echo "############ API Gateway lockdown ############"
echo "--- unauthenticated direct call (expect 403 Missing Authentication Token) ---"
curl -s -o /dev/stderr -w 'HTTP %{http_code}\n' "$API_ENDPOINT/dbinfo" 2>&1
echo "--- SigV4 direct call as a non-gateway principal (expect 403 explicit deny) ---"
python3 - <<'PY'
import urllib.request, urllib.error, boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
st={}
for l in open('state.env'):
    l=l.strip()
    if '=' in l: k,v=l.split('=',1); st[k]=v
url=st['API_ENDPOINT']+'/dbinfo'
c=boto3.Session().get_credentials().get_frozen_credentials()
r=AWSRequest(method='GET',url=url,data=b'')
SigV4Auth(c,'execute-api',st['API_ENDPOINT'].split('.')[2]).add_auth(r)
try:
    with urllib.request.urlopen(urllib.request.Request(url,headers=dict(r.headers)),timeout=30) as resp:
        print('HTTP',resp.status,'NOT blocked')
except urllib.error.HTTPError as e:
    print('HTTP',e.code,e.read().decode()[:220])
PY
echo "--- effective resource policy ---"
aws apigateway get-rest-api --rest-api-id "$API_ID" --region "$REGION" --query policy --output text
} 2>&1 | tee "$OUT"

echo
log "Evidence written to $OUT"
