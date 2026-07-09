#!/usr/bin/env bash
# Delete every AWS resource this benchmark created, as recorded in
# deployments.json: 3 agent runtimes -> ECR repo -> IAM role.
#
# Usage:
#   scripts/cleanup.sh --dry-run   # list what would be deleted (no changes)
#   scripts/cleanup.sh --yes       # actually delete
#
# Refuses to run without one of the two flags. Never touches resources that
# are not recorded in deployments.json / not prefixed coldstart_ping.
set -euo pipefail
cd "$(dirname "$0")/.."

MODE="${1:-}"
if [ "$MODE" != "--dry-run" ] && [ "$MODE" != "--yes" ]; then
    echo "Usage: $0 --dry-run | --yes" >&2
    echo "  --dry-run  list the resources that would be deleted" >&2
    echo "  --yes      delete them (runtimes -> ECR repo -> IAM role)" >&2
    exit 2
fi

if [ ! -f deployments.json ]; then
    echo "ERROR: deployments.json not found — nothing recorded to clean up." >&2
    exit 2
fi

REGION=$(python3 -c "import json; print(json.load(open('deployments.json'))['region'])")
REPO=agentcore-coldstart-pingpong
ROLE_NAME=AgentCoreColdstartRole
POLICY_NAME=AgentCoreColdstartPolicy

mapfile -t RUNTIME_ARNS < <(python3 -c "
import json
d = json.load(open('deployments.json'))
for rt in d['runtimes'].values():
    print(rt['arn'])
")

echo "Resources recorded in deployments.json (region $REGION):"
for arn in "${RUNTIME_ARNS[@]}"; do
    echo "  runtime:  $arn"
done
echo "  ecr repo: $REPO (all images, --force)"
echo "  iam role: $ROLE_NAME (inline policy $POLICY_NAME)"

if [ "$MODE" = "--dry-run" ]; then
    echo "DRY RUN — nothing deleted."
    exit 0
fi

echo
for arn in "${RUNTIME_ARNS[@]}"; do
    name="${arn##*/}"           # e.g. coldstart_ping_500mb-hS5A2058Rp
    case "$name" in
        coldstart_ping_*) ;;
        *) echo "SKIP (not coldstart_ping-prefixed): $arn"; continue ;;
    esac
    echo "Deleting runtime $name"
    aws bedrock-agentcore-control delete-agent-runtime --region "$REGION" \
        --agent-runtime-id "$name" >/dev/null 2>&1 \
        && echo "  deleted" || echo "  already gone or delete failed (continuing)"
done

echo "Deleting ECR repo $REPO"
aws ecr delete-repository --repository-name "$REPO" --region "$REGION" --force >/dev/null 2>&1 \
    && echo "  deleted" || echo "  already gone or delete failed (continuing)"

echo "Deleting IAM role $ROLE_NAME"
aws iam delete-role-policy --role-name "$ROLE_NAME" --policy-name "$POLICY_NAME" 2>/dev/null \
    && echo "  inline policy deleted" || echo "  inline policy already gone"
aws iam delete-role --role-name "$ROLE_NAME" 2>/dev/null \
    && echo "  role deleted" || echo "  role already gone"

echo "Cleanup complete. deployments.json kept as a record (delete it manually if re-deploying fresh)."
