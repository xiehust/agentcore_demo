#!/usr/bin/env bash
# Phase 1: a deliberately ISOLATED VPC (no IGW, no NAT, no internet egress at all)
# containing a private MySQL RDS instance. That RDS instance is the "private
# resource" both workarounds must reach from AgentCore Gateway.
#
# The absence of an internet gateway is the whole point: if the workarounds work
# here, they work in a locked-down / China-region-style network.
source "$(dirname "$0")/lib.sh"

# ---------- VPC ----------
if [[ -z "${VPC_ID:-}" ]]; then
  log "Creating isolated VPC 10.30.0.0/16 (no IGW, no NAT)"
  VPC_ID=$(aws ec2 create-vpc --cidr-block 10.30.0.0/16 --region "$REGION" \
    --tag-specifications "ResourceType=vpc,Tags=[{Key=Name,Value=$PREFIX-vpc}]" \
    --query Vpc.VpcId --output text)
  aws ec2 modify-vpc-attribute --vpc-id "$VPC_ID" --enable-dns-hostnames --region "$REGION"
  save VPC_ID "$VPC_ID"
fi
ok "VPC $VPC_ID"

mk_subnet() { # name cidr az var
  local name="$1" cidr="$2" az="$3" var="$4"
  if [[ -z "${!var:-}" ]]; then
    local id
    id=$(aws ec2 create-subnet --vpc-id "$VPC_ID" --cidr-block "$cidr" \
      --availability-zone "$az" --region "$REGION" \
      --tag-specifications "ResourceType=subnet,Tags=[{Key=Name,Value=$name}]" \
      --query Subnet.SubnetId --output text)
    save "$var" "$id"
  fi
  ok "subnet ${!var} ($name)"
}
mk_subnet "$PREFIX-private-a" 10.30.11.0/24 "$AZ_A" SUBNET_PRIV_A
mk_subnet "$PREFIX-private-b" 10.30.12.0/24 "$AZ_B" SUBNET_PRIV_B

# ---------- route table (no default route -> no egress) ----------
if [[ -z "${RT_PRIV:-}" ]]; then
  RT_PRIV=$(aws ec2 create-route-table --vpc-id "$VPC_ID" --region "$REGION" \
    --tag-specifications "ResourceType=route-table,Tags=[{Key=Name,Value=$PREFIX-rt-private}]" \
    --query RouteTable.RouteTableId --output text)
  aws ec2 associate-route-table --route-table-id "$RT_PRIV" --subnet-id "$SUBNET_PRIV_A" --region "$REGION" >/dev/null
  aws ec2 associate-route-table --route-table-id "$RT_PRIV" --subnet-id "$SUBNET_PRIV_B" --region "$REGION" >/dev/null
  save RT_PRIV "$RT_PRIV"
fi
ok "private route table $RT_PRIV (no 0.0.0.0/0 route)"

# ---------- security groups ----------
mk_sg() { # name desc var
  local name="$1" desc="$2" var="$3"
  if [[ -z "${!var:-}" ]]; then
    local id
    id=$(aws ec2 create-security-group --group-name "$name" --description "$desc" \
      --vpc-id "$VPC_ID" --region "$REGION" --query GroupId --output text)
    save "$var" "$id"
  fi
  ok "sg ${!var} ($name)"
}
mk_sg "$PREFIX-lambda-sg" "Lambda ENIs reaching RDS" SG_LAMBDA
mk_sg "$PREFIX-app-sg"    "In-VPC HTTP app behind the NLB" SG_APP
mk_sg "$PREFIX-rds-sg"    "Private RDS MySQL" SG_RDS
mk_sg "$PREFIX-vpce-sg"   "Interface VPC endpoints" SG_VPCE

# RDS accepts 3306 only from the Lambda SG and the in-VPC app SG.
for src in "$SG_LAMBDA" "$SG_APP"; do
  aws ec2 authorize-security-group-ingress --group-id "$SG_RDS" --protocol tcp --port 3306 \
    --source-group "$src" --region "$REGION" >/dev/null 2>&1 || true
done
# NLB health checks and forwarded traffic originate inside the VPC.
aws ec2 authorize-security-group-ingress --group-id "$SG_APP" --protocol tcp --port 8080 \
  --cidr 10.30.0.0/16 --region "$REGION" >/dev/null 2>&1 || true
# Interface endpoints accept HTTPS from inside the VPC.
aws ec2 authorize-security-group-ingress --group-id "$SG_VPCE" --protocol tcp --port 443 \
  --cidr 10.30.0.0/16 --region "$REGION" >/dev/null 2>&1 || true
ok "security group rules applied"

# ---------- VPC endpoints (replace the NAT gateway) ----------
# S3 gateway endpoint: free, used to ship the EC2 bootstrap payload into the
# isolated subnets. SSM interface endpoints: let us shell into the instance for
# debugging without SSH, a bastion, or any internet route.
if [[ -z "${VPCE_S3:-}" ]]; then
  log "Creating S3 gateway endpoint"
  VPCE_S3=$(aws ec2 create-vpc-endpoint --vpc-id "$VPC_ID" \
    --service-name "com.amazonaws.$REGION.s3" --vpc-endpoint-type Gateway \
    --route-table-ids "$RT_PRIV" --region "$REGION" \
    --query 'VpcEndpoint.VpcEndpointId' --output text)
  save VPCE_S3 "$VPCE_S3"
fi
ok "S3 gateway endpoint $VPCE_S3"

for svc in ssm ssmmessages ec2messages; do
  var="VPCE_$(echo "$svc" | tr '[:lower:]' '[:upper:]')"
  if [[ -z "${!var:-}" ]]; then
    log "Creating interface endpoint for $svc"
    id=$(aws ec2 create-vpc-endpoint --vpc-id "$VPC_ID" \
      --service-name "com.amazonaws.$REGION.$svc" --vpc-endpoint-type Interface \
      --subnet-ids "$SUBNET_PRIV_A" "$SUBNET_PRIV_B" \
      --security-group-ids "$SG_VPCE" --private-dns-enabled \
      --region "$REGION" --query 'VpcEndpoint.VpcEndpointId' --output text)
    save "$var" "$id"
  fi
  ok "interface endpoint ${!var} ($svc)"
done

# ---------- RDS ----------
if [[ -z "${DB_PASS:-}" ]]; then
  save DB_PASS "Demo$(openssl rand -hex 8)Aa1"
fi
save DB_NAME "$DB_NAME"
save DB_USER "$DB_USER"

if ! aws rds describe-db-subnet-groups --db-subnet-group-name "$PREFIX-subnets" \
      --region "$REGION" >/dev/null 2>&1; then
  log "Creating DB subnet group"
  aws rds create-db-subnet-group --db-subnet-group-name "$PREFIX-subnets" \
    --db-subnet-group-description "private subnets for $PREFIX" \
    --subnet-ids "$SUBNET_PRIV_A" "$SUBNET_PRIV_B" --region "$REGION" >/dev/null
fi
ok "db subnet group $PREFIX-subnets"

DB_ID="$PREFIX-mysql"
if ! aws rds describe-db-instances --db-instance-identifier "$DB_ID" --region "$REGION" >/dev/null 2>&1; then
  log "Creating RDS MySQL instance (publicly-accessible=false) — takes several minutes"
  aws rds create-db-instance \
    --db-instance-identifier "$DB_ID" \
    --db-instance-class db.t4g.micro \
    --engine mysql --engine-version 8.0.42 \
    --master-username "$DB_USER" --master-user-password "$DB_PASS" \
    --allocated-storage 20 --storage-type gp3 \
    --db-name "$DB_NAME" \
    --db-subnet-group-name "$PREFIX-subnets" \
    --vpc-security-group-ids "$SG_RDS" \
    --no-publicly-accessible \
    --backup-retention-period 0 --no-multi-az \
    --no-auto-minor-version-upgrade \
    --region "$REGION" >/dev/null
fi
save DB_ID "$DB_ID"
ok "RDS instance $DB_ID requested"

log "Waiting for RDS to become available"
aws rds wait db-instance-available --db-instance-identifier "$DB_ID" --region "$REGION"
read -r DB_HOST PUBLIC < <(aws rds describe-db-instances --db-instance-identifier "$DB_ID" \
  --region "$REGION" --query 'DBInstances[0].[Endpoint.Address,PubliclyAccessible]' --output text)
save DB_HOST "$DB_HOST"
ok "RDS endpoint $DB_HOST (PubliclyAccessible=$PUBLIC)"

log "Phase 1 complete. State written to $STATE_FILE"
