#!/usr/bin/env bash
# Shared config. Reuses the isolated VPC / private RDS built by
# 11-vpc-no-egress-workaround, so this project only adds the IdP + interceptor.
set -euo pipefail
export AWS_PAGER=""

REGION="${REGION:-us-east-2}"
PREFIX="${PREFIX:-acdemo-idp}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_STATE="$ROOT_DIR/../11-vpc-no-egress-workaround/state.env"
STATE_FILE="$ROOT_DIR/state.env"
touch "$STATE_FILE"

[[ -f "$BASE_STATE" ]] || {
  echo "FATAL: $BASE_STATE not found — run 11-vpc-no-egress-workaround first" >&2
  exit 1; }
# shellcheck disable=SC1090
source "$BASE_STATE"      # VPC_ID, SUBNET_PRIV_*, SG_*, DB_*, BUCKET
# shellcheck disable=SC1090
source "$STATE_FILE"      # anything this project has already created

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

# IdP demo settings
IDP_PORT="${IDP_PORT:-8081}"
IDP_AUDIENCE="${IDP_AUDIENCE:-agentcore-gateway}"
IDP_CLIENT_ID="${IDP_CLIENT_ID:-order-desk-agent}"
IDP_KID="${IDP_KID:-demo-key-1}"
REQUIRED_SCOPE="${REQUIRED_SCOPE:-orders.read}"

save() {
  local key="$1" val="$2"
  if grep -q "^${key}=" "$STATE_FILE" 2>/dev/null; then
    sed -i "s|^${key}=.*|${key}=${val}|" "$STATE_FILE"
  else
    echo "${key}=${val}" >> "$STATE_FILE"
  fi
  export "${key}=${val}"
}

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  !!\033[0m %s\n' "$*"; }
