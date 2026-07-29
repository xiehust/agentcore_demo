#!/usr/bin/env bash
# Phase 1: RSA keypair + a private OIDC IdP on EC2 in the isolated VPC.
source "$(dirname "$0")/lib.sh"

KEY_DIR="$ROOT_DIR/build/keys"
mkdir -p "$KEY_DIR" "$ROOT_DIR/build"

# ---------- signing keys ----------
# Generated locally so the test client can mint tokens with deliberately wrong
# claims (expired / wrong aud / wrong issuer) for the negative tests. The second
# key is never given to the IdP -- it exists only to forge an invalid signature.
if [[ ! -f "$KEY_DIR/private_key.pem" ]]; then
  log "Generating RSA keypairs (real + attacker)"
  openssl genrsa -out "$KEY_DIR/private_key.pem" 2048 2>/dev/null
  openssl genrsa -out "$KEY_DIR/attacker_key.pem" 2048 2>/dev/null
fi
ok "keys in $KEY_DIR"

if [[ -z "${IDP_CLIENT_SECRET:-}" ]]; then
  save IDP_CLIENT_SECRET "$(openssl rand -hex 16)"
fi

# ---------- dependency bundle ----------
# The instance has no internet route, so wheels are shipped in via S3 over the
# S3 gateway endpoint. --platform is required: this build host is arm64 but the
# t3.micro instance and the Lambdas are x86_64.
log "Building x86_64 dependency bundle"
BUNDLE="$ROOT_DIR/build/idpdeps"
rm -rf "$BUNDLE"; mkdir -p "$BUNDLE"
# The AL2023 AMI ships Python 3.9 and has NO pip, so wheels are resolved for cp39
# here and simply unzipped on the instance (a wheel is just a zip). Letting pip
# pick the cryptography version keeps us on one with a cp39-compatible abi3 wheel.
python3 -m pip download --quiet --dest "$BUNDLE/wheels" \
  --platform manylinux2014_x86_64 --python-version 3.9 --only-binary=:all: \
  "pyjwt[crypto]==2.10.1" >/dev/null
ok "wheels: $(ls "$BUNDLE/wheels" | tr '\n' ' ')"
cp "$ROOT_DIR/idp/idp_server.py" "$BUNDLE/"
cp "$KEY_DIR/private_key.pem" "$BUNDLE/"
tar -czf "$ROOT_DIR/build/idp.tar.gz" -C "$BUNDLE" .
aws s3 cp "$ROOT_DIR/build/idp.tar.gz" "s3://$BUCKET/idp.tar.gz" --region "$REGION" >/dev/null
IDP_PAYLOAD_URL=$(aws s3 presign "s3://$BUCKET/idp.tar.gz" --expires-in 604800 --region "$REGION")
ok "bundle uploaded ($(du -h "$ROOT_DIR/build/idp.tar.gz" | cut -f1))"

# ---------- security group ----------
if [[ -z "${SG_IDP:-}" ]]; then
  SG_IDP=$(aws ec2 create-security-group --group-name "$PREFIX-idp-sg" \
    --description "Private OIDC IdP" --vpc-id "$VPC_ID" --region "$REGION" \
    --query GroupId --output text)
  save SG_IDP "$SG_IDP"
fi
# Only the Lambda ENIs may reach the IdP. Nothing else, and nothing public.
aws ec2 authorize-security-group-ingress --group-id "$SG_IDP" --protocol tcp \
  --port "$IDP_PORT" --source-group "$SG_LAMBDA" --region "$REGION" >/dev/null 2>&1 || true
ok "sg $SG_IDP (ingress $IDP_PORT from $SG_LAMBDA only)"

# ---------- instance ----------
if [[ -z "${IDP_INSTANCE_ID:-}" ]]; then
  AMI=$(aws ssm get-parameter --region "$REGION" \
    --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-6.1-x86_64 \
    --query Parameter.Value --output text)
  log "Launching private IdP instance"
  cat > "$ROOT_DIR/build/idp-userdata.sh" <<EOF
#!/bin/bash
set -xe
mkdir -p /opt/idp
python3 -c "import urllib.request;urllib.request.urlretrieve('$IDP_PAYLOAD_URL','/tmp/idp.tar.gz')"
tar -xzf /tmp/idp.tar.gz -C /opt/idp
# No pip on this AMI: extract each wheel straight into the site directory.
mkdir -p /opt/idp/site
for w in /opt/idp/wheels/*.whl; do python3 -m zipfile -e "\$w" /opt/idp/site; done
chmod 600 /opt/idp/private_key.pem
PRIVATE_IP=\$(hostname -I | awk '{print \$1}')
cat > /etc/systemd/system/idp.service <<UNIT
[Unit]
Description=Demo private OIDC IdP
[Service]
Environment=IDP_PORT=$IDP_PORT
Environment=IDP_ISSUER=http://\$PRIVATE_IP:$IDP_PORT
Environment=IDP_AUDIENCE=$IDP_AUDIENCE
Environment=IDP_CLIENT_ID=$IDP_CLIENT_ID
Environment=IDP_CLIENT_SECRET=$IDP_CLIENT_SECRET
Environment=IDP_KID=$IDP_KID
Environment=IDP_KEY_PATH=/opt/idp/private_key.pem
Environment=PYTHONPATH=/opt/idp/site
ExecStart=/usr/bin/python3 /opt/idp/idp_server.py
Restart=always
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now idp.service
EOF
  IDP_INSTANCE_ID=$(aws ec2 run-instances --region "$REGION" \
    --image-id "$AMI" --instance-type t3.micro \
    --subnet-id "$SUBNET_PRIV_A" --security-group-ids "$SG_IDP" \
    --no-associate-public-ip-address \
    --iam-instance-profile "Name=acdemo-noegress-ec2-role" \
    --user-data "file://$ROOT_DIR/build/idp-userdata.sh" \
    --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$PREFIX-idp}]" \
    --query 'Instances[0].InstanceId' --output text)
  save IDP_INSTANCE_ID "$IDP_INSTANCE_ID"
fi
aws ec2 wait instance-running --instance-ids "$IDP_INSTANCE_ID" --region "$REGION"
IDP_IP=$(aws ec2 describe-instances --instance-ids "$IDP_INSTANCE_ID" --region "$REGION" \
  --query 'Reservations[0].Instances[0].PrivateIpAddress' --output text)
save IDP_IP "$IDP_IP"
save IDP_ISSUER "http://$IDP_IP:$IDP_PORT"
save IDP_JWKS_URL "http://$IDP_IP:$IDP_PORT/jwks"
save IDP_TOKEN_URL "http://$IDP_IP:$IDP_PORT/token"
save IDP_AUDIENCE "$IDP_AUDIENCE"
save IDP_CLIENT_ID "$IDP_CLIENT_ID"
save IDP_KID "$IDP_KID"
ok "instance $IDP_INSTANCE_ID at $IDP_IP (no public IP)"

PUBLIC_IP=$(aws ec2 describe-instances --instance-ids "$IDP_INSTANCE_ID" --region "$REGION" \
  --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)
ok "PublicIpAddress=$PUBLIC_IP (expected None)"

log "Phase 1 complete. The IdP needs ~1-2 min to finish bootstrapping."
