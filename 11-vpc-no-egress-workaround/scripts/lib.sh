#!/usr/bin/env bash
# Shared configuration + tiny state store for the no-egress workaround demo.
set -euo pipefail

export AWS_PAGER=""
REGION="${REGION:-us-east-2}"
AZ_A="${AZ_A:-us-east-2a}"
AZ_B="${AZ_B:-us-east-2b}"
PREFIX="${PREFIX:-acdemo-noegress}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_FILE="$ROOT_DIR/state.env"
touch "$STATE_FILE"
# shellcheck disable=SC1090
source "$STATE_FILE"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

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

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  !!\033[0m %s\n' "$*"; }
