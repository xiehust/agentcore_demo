#!/usr/bin/env bash
# Shared config. Reuses the isolated VPC / private RDS built by
# 11-vpc-no-egress-workaround, so this project only adds the IdP + interceptor.
set -euo pipefail
export AWS_PAGER=""

# Pick the account with AWS_PROFILE (e.g. AWS_PROFILE=zhy REGION=cn-northwest-1).
REGION="${REGION:-us-east-2}"
PREFIX="${PREFIX:-acdemo-idp}"
export AWS_REGION="$REGION" AWS_DEFAULT_REGION="$REGION"
case "$REGION" in
  cn-*) PARTITION="aws-cn"; DNS_SUFFIX="amazonaws.com.cn" ;;
  *)    PARTITION="aws";    DNS_SUFFIX="amazonaws.com" ;;
esac

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Python with pyjwt + boto3 (see README "Reproducing").
PY="${PYTHON:-$ROOT_DIR/.venv/bin/python}"
# Where 04-verify.py / 05-collect-evidence.sh write; override to keep a baseline.
RESULTS_DIR="${RESULTS_DIR:-$ROOT_DIR/results}"
export RESULTS_DIR
BASE_STATE="$ROOT_DIR/../11-vpc-no-egress-workaround/state.env"
STATE_FILE="$ROOT_DIR/state.env"
touch "$STATE_FILE"

[[ -f "$BASE_STATE" ]] || {
  echo "FATAL: $BASE_STATE not found — run 11-vpc-no-egress-workaround first" >&2
  exit 1; }
# shellcheck disable=SC1090
source "$BASE_STATE"      # VPC_ID, SUBNET_PRIV_*, SG_*, DB_*, BUCKET, STATE_*
BASE_ACCOUNT="${STATE_ACCOUNT:-}" BASE_REGION="${STATE_REGION:-}"
unset STATE_ACCOUNT STATE_REGION
# shellcheck disable=SC1090
source "$STATE_FILE"      # anything this project has already created

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

# Account/region guard: EXPECTED_ACCOUNT (optional) must match the caller, and
# both this project's state and the base VPC state must belong to the same
# account/region as the current caller.
if [[ -n "${EXPECTED_ACCOUNT:-}" && "$ACCOUNT_ID" != "$EXPECTED_ACCOUNT" ]]; then
  echo "FATAL: caller account $ACCOUNT_ID != EXPECTED_ACCOUNT $EXPECTED_ACCOUNT" >&2; exit 1
fi
for pair in "${BASE_ACCOUNT}:$ACCOUNT_ID:base-account" "${STATE_ACCOUNT:-}:$ACCOUNT_ID:account" \
            "${BASE_REGION}:$REGION:base-region" "${STATE_REGION:-}:$REGION:region"; do
  IFS=: read -r recorded current what <<<"$pair"
  if [[ -n "$recorded" && "$recorded" != "$current" ]]; then
    echo "FATAL: state $what is $recorded but current is $current" >&2; exit 1
  fi
done

# IdP demo settings
IDP_PORT="${IDP_PORT:-8081}"
IDP_AUDIENCE="${IDP_AUDIENCE:-agentcore-gateway}"
IDP_CLIENT_ID="${IDP_CLIENT_ID:-order-desk-agent}"
IDP_KID="${IDP_KID:-demo-key-1}"
REQUIRED_SCOPE="${REQUIRED_SCOPE:-orders.read}"

# Gateway inbound auth. NONE is the original design, but China regions reject
# authorizerType=NONE ("NONE AuthorizerType is not valid"), so there the gateway
# uses AWS_IAM. SigV4 then owns the Authorization header, so the business JWT
# travels in TOKEN_HEADER instead and the interceptor reads it from there.
if [[ -z "${INBOUND_AUTH:-}" ]]; then
  [[ "$PARTITION" == "aws-cn" ]] && INBOUND_AUTH="AWS_IAM" || INBOUND_AUTH="NONE"
fi
if [[ -z "${TOKEN_HEADER:-}" ]]; then
  [[ "$INBOUND_AUTH" == "AWS_IAM" ]] && TOKEN_HEADER="X-Idp-Authorization" || TOKEN_HEADER="Authorization"
fi

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

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  !!\033[0m %s\n' "$*"; }
