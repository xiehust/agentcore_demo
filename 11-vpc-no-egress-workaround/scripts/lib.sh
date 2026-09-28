#!/usr/bin/env bash
# Shared configuration + tiny state store for the no-egress workaround demo.
set -euo pipefail

export AWS_PAGER=""
# Pick the account with AWS_PROFILE (e.g. AWS_PROFILE=zhy REGION=cn-northwest-1).
REGION="${REGION:-us-east-2}"
AZ_A="${AZ_A:-${REGION}a}"
AZ_B="${AZ_B:-${REGION}b}"
PREFIX="${PREFIX:-acdemo-noegress}"
export AWS_REGION="$REGION" AWS_DEFAULT_REGION="$REGION"

# China regions live in their own partition with their own DNS suffix.
case "$REGION" in
  cn-*) PARTITION="aws-cn"; DNS_SUFFIX="amazonaws.com.cn" ;;
  *)    PARTITION="aws";    DNS_SUFFIX="amazonaws.com" ;;
esac

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_FILE="$ROOT_DIR/state.env"
touch "$STATE_FILE"
# shellcheck disable=SC1090
source "$STATE_FILE"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

# Account/region guard: EXPECTED_ACCOUNT (optional) must match the caller, and a
# resumed run must stay on the account/region recorded in state.env.
if [[ -n "${EXPECTED_ACCOUNT:-}" && "$ACCOUNT_ID" != "$EXPECTED_ACCOUNT" ]]; then
  echo "FATAL: caller account $ACCOUNT_ID != EXPECTED_ACCOUNT $EXPECTED_ACCOUNT" >&2; exit 1
fi
if [[ -n "${STATE_ACCOUNT:-}" && "$STATE_ACCOUNT" != "$ACCOUNT_ID" ]]; then
  echo "FATAL: state.env belongs to account $STATE_ACCOUNT, caller is $ACCOUNT_ID" >&2; exit 1
fi
if [[ -n "${STATE_REGION:-}" && "$STATE_REGION" != "$REGION" ]]; then
  echo "FATAL: state.env belongs to region $STATE_REGION, REGION is $REGION" >&2; exit 1
fi

# Database credentials for the demo. The password is generated once and kept in
# state.env so every script (and the Lambda / EC2 bootstrap) uses the same one.
DB_NAME="${DB_NAME:-agentdemo}"
DB_USER="${DB_USER:-agentadmin}"

# save KEY VALUE -> persist to state.env and export into the current shell
save() {
  local key="$1" val="$2"
  if grep -q "^${key}=" "$STATE_FILE" 2>/dev/null; then
    sed -i "s|^${key}=.*|${key}=${val}|" "$STATE_FILE"
  else
    echo "${key}=${val}" >> "$STATE_FILE"
  fi
  export "${key}=${val}"
}
save STATE_ACCOUNT "$ACCOUNT_ID"
save STATE_REGION "$REGION"

# vpce_service SVC -> the endpoint service name for SVC in this region. Most are
# com.amazonaws.<region>.<svc>, but in China some (lambda, sts, s3 interface) are
# published as cn.com.amazonaws.<region>.<svc>.
vpce_service() {
  local svc="$1" name
  for name in "com.amazonaws.$REGION.$svc" "cn.com.amazonaws.$REGION.$svc"; do
    if aws ec2 describe-vpc-endpoint-services --service-names "$name" \
         --region "$REGION" >/dev/null 2>&1; then
      echo "$name"; return 0
    fi
  done
  echo "FATAL: no VPC endpoint service for $svc in $REGION" >&2; return 1
}

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  !!\033[0m %s\n' "$*"; }
