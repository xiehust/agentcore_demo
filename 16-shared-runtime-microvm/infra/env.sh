#!/usr/bin/env bash
# Shared names/settings for infra/deploy.sh and infra/destroy.sh.
# Everything is overridable through the environment.

export AWS_PAGER=""
REGION="${REGION:-us-west-2}"
PREFIX="${PREFIX:-srpool}"                       # resource-name prefix
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
POOL_CONFIG="${POOL_CONFIG:-${ROOT}/pool.json}"
BUILD_DIR="${ROOT}/build"

ACCOUNT_ID="${ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text)}"

# --- images -------------------------------------------------------------------
REPO="${REPO:-launchpad-agents}"
RUNTIME_TAG="${RUNTIME_TAG:-${PREFIX}-runtime-v1}"
ROUTER_TAG="${ROUTER_TAG:-${PREFIX}-router-v1}"
ECR_HOST="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
RUNTIME_IMAGE_URI="${ECR_HOST}/${REPO}:${RUNTIME_TAG}"
ROUTER_IMAGE_URI="${ECR_HOST}/${REPO}:${ROUTER_TAG}"
SKIP_IMAGE_BUILD="${SKIP_IMAGE_BUILD:-0}"

# --- AgentCore Runtime ----------------------------------------------------------
RUNTIME_NAME="${RUNTIME_NAME:-${PREFIX}_runtime}"      # [a-zA-Z][a-zA-Z0-9_]{0,47}
MODEL_ID="${MODEL_ID:-global.anthropic.claude-haiku-4-5-20251001-v1:0}"
MAX_PARALLEL_AGENTS="${MAX_PARALLEL_AGENTS:-10}"
MAX_TURNS="${MAX_TURNS:-64}"
IDLE_TIMEOUT_S="${IDLE_TIMEOUT_S:-900}"                # microVM idleRuntimeSessionTimeout
MAX_LIFETIME_S="${MAX_LIFETIME_S:-28800}"              # microVM maxLifetime (8h max)

# --- Workspace storage: Amazon S3 Files (bring-your-own, shared across sessions)
# User workspaces live at <bucket>/users/<slug>/ and are mounted read-write into
# every session at MOUNT_PATH. Requires the Runtime in VPC mode (private subnets
# + NAT gateway for Bedrock/ECR egress) and S3 Files mount targets in those AZs.
WORKSPACE_BUCKET="${WORKSPACE_BUCKET:-${PREFIX}-workspaces-${ACCOUNT_ID}-${REGION}}"
S3FILES_ROLE_NAME="${S3FILES_ROLE_NAME:-${PREFIX}-s3files-role}"
S3FILES_ROOT="${S3FILES_ROOT:-/users}"                 # access-point root inside the file system
MOUNT_PATH="${MOUNT_PATH:-/mnt/users}"                 # must be /mnt/<name>
USERS_ROOT="${USERS_ROOT:-${MOUNT_PATH}}"
RUNTIME_SG_NAME="${RUNTIME_SG_NAME:-${PREFIX}-runtime-sg}"
MOUNT_SG_NAME="${MOUNT_SG_NAME:-${PREFIX}-s3files-mt-sg}"
# Either reuse existing private subnets that already route through a NAT gateway
# (RUNTIME_SUBNET_IDS, comma-separated; their VPC is used for the Runtime and
# the S3 Files mount targets), or leave empty to create private subnets + NAT
# in the default VPC (needs one free Elastic IP).
RUNTIME_SUBNET_IDS="${RUNTIME_SUBNET_IDS:-}"
PRIVATE_SUBNET_CIDRS="${PRIVATE_SUBNET_CIDRS:-172.31.64.0/24,172.31.65.0/24}"

# --- IAM ----------------------------------------------------------------------
RUNTIME_ROLE_NAME="${RUNTIME_ROLE_NAME:-${PREFIX}-runtime-role}"
ECS_EXEC_ROLE_NAME="${ECS_EXEC_ROLE_NAME:-${PREFIX}-ecs-exec-role}"
ROUTER_TASK_ROLE_NAME="${ROUTER_TASK_ROLE_NAME:-${PREFIX}-router-task-role}"
LAMBDA_ROLE_NAME="${LAMBDA_ROLE_NAME:-${PREFIX}-reconciler-role}"

# --- DynamoDB -----------------------------------------------------------------
TABLE_NAME="${TABLE_NAME:-${PREFIX}-session-pool}"

# --- Router (ECS Fargate + internal ALB) --------------------------------------
CLUSTER_NAME="${CLUSTER_NAME:-${PREFIX}-cluster}"
SERVICE_NAME="${SERVICE_NAME:-${PREFIX}-router}"
TASK_FAMILY="${TASK_FAMILY:-${PREFIX}-router}"
LOG_GROUP="${LOG_GROUP:-/ecs/${PREFIX}-router}"
ALB_NAME="${ALB_NAME:-${PREFIX}-alb}"                  # <= 32 chars
TG_NAME="${TG_NAME:-${PREFIX}-router-tg}"
ALB_SG_NAME="${ALB_SG_NAME:-${PREFIX}-alb-sg}"
TASK_SG_NAME="${TASK_SG_NAME:-${PREFIX}-task-sg}"
ALB_IDLE_TIMEOUT_S="${ALB_IDLE_TIMEOUT_S:-4000}"       # long SSE streams
ROUTER_CPU="${ROUTER_CPU:-1024}"
ROUTER_MEMORY="${ROUTER_MEMORY:-2048}"
ROUTER_DESIRED_COUNT="${ROUTER_DESIRED_COUNT:-1}"      # >1 is fine: the router is stateless

# Scheduler knobs (SESSION_POOL_ARCHITECTURE.zh.md §5, §9, §11, §13)
MAX_INFLIGHT="${MAX_INFLIGHT:-10}"
TARGET_INFLIGHT="${TARGET_INFLIGHT:-7}"
MAX_SESSIONS="${MAX_SESSIONS:-6}"
MIN_WARM_SESSIONS="${MIN_WARM_SESSIONS:-1}"
AFFINITY_TTL_S="${AFFINITY_TTL_S:-1800}"
QUEUE_WAIT_S="${QUEUE_WAIT_S:-45}"
IDLE_STOP_S="${IDLE_STOP_S:-300}"
APP_VERSION="${APP_VERSION:-v1}"
CONTEXT_EXTERNALIZED="${CONTEXT_EXTERNALIZED:-1}"     # workspaces on S3 Files: resumes may migrate

# --- Reconciler (EventBridge + Lambda) ----------------------------------------
LAMBDA_NAME="${LAMBDA_NAME:-${PREFIX}-reconciler}"
RULE_NAME="${RULE_NAME:-${PREFIX}-reconciler-schedule}"
RECONCILE_RATE="${RECONCILE_RATE:-rate(1 minute)}"

# --- Networking ---------------------------------------------------------------
# The ALB is internal and only admits CLIENT_CIDR (default: this EC2's private IP).
detect_private_ip() {
  local token
  token="$(curl -s -m 2 -X PUT "http://169.254.169.254/latest/api/token" \
    -H "X-aws-ec2-metadata-token-ttl-seconds: 60" || true)"
  [[ -n "${token}" ]] || return 1
  curl -s -m 2 -H "X-aws-ec2-metadata-token: ${token}" \
    http://169.254.169.254/latest/meta-data/local-ipv4
}
if [[ -z "${CLIENT_CIDR:-}" ]]; then
  _ip="$(detect_private_ip || true)"
  [[ -n "${_ip}" ]] && CLIENT_CIDR="${_ip}/32"
fi
CLIENT_CIDR="${CLIENT_CIDR:-}"
VPC_ID="${VPC_ID:-}"
SUBNET_IDS="${SUBNET_IDS:-}"   # comma-separated; default: the VPC's default-for-az subnets

log() { printf '\n== %s ==\n' "$*"; }
die() { echo "ERROR: $*" >&2; exit 1; }
