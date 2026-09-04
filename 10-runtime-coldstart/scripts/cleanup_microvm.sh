#!/usr/bin/env bash
# Delete every AWS resource the Lambda MicroVMs track created, as recorded in
# deployments_microvm.json: running/suspended MicroVMs of the 3 images ->
# the 3 MicroVM images (all versions) -> S3 artifact bucket -> IAM roles.
#
# Usage:
#   scripts/cleanup_microvm.sh --dry-run   # list what would be deleted (no changes)
#   scripts/cleanup_microvm.sh --yes       # actually delete
#
# Refuses to run without one of the two flags. Never touches resources that
# are not recorded in deployments_microvm.json / not prefixed coldstart-ping-microvm.
set -euo pipefail
cd "$(dirname "$0")/.."

MODE="${1:-}"
if [ "$MODE" != "--dry-run" ] && [ "$MODE" != "--yes" ]; then
    echo "Usage: $0 --dry-run | --yes" >&2
    echo "  --dry-run  list the resources that would be deleted" >&2
    echo "  --yes      delete them (microvms -> images -> S3 bucket -> IAM roles)" >&2
    exit 2
fi

if [ ! -f deployments_microvm.json ]; then
    echo "ERROR: deployments_microvm.json not found — nothing recorded to clean up." >&2
    exit 2
fi

REGION=$(python3 -c "import json; print(json.load(open('deployments_microvm.json'))['region'])")
BUCKET=$(python3 -c "import json; print(json.load(open('deployments_microvm.json'))['bucket'])")
BUILD_ROLE_NAME=LambdaMicrovmColdstartBuildRole
EXEC_ROLE_NAME=LambdaMicrovmColdstartExecRole
POLICY_NAME=LambdaMicrovmColdstartPolicy

mapfile -t IMAGE_ARNS < <(python3 -c "
import json
d = json.load(open('deployments_microvm.json'))
for img in d['images'].values():
    print(img['arn'])
")

echo "Resources recorded in deployments_microvm.json (region $REGION):"
declare -a VM_IDS=()
for arn in "${IMAGE_ARNS[@]}"; do
    echo "  image:    $arn"
    # list-microvms is filterable by image; only RUNNING/SUSPENDED VMs cost money
    while read -r vm st; do
        [ -z "$vm" ] || [ "$vm" = "None" ] && continue
        case "$st" in TERMINATED|TERMINATING) continue ;; esac
        echo "    microvm: $vm ($st)"
        VM_IDS+=("$vm")
    done < <(aws lambda-microvms list-microvms --region "$REGION" --image-identifier "$arn" \
        --query 'items[].[microvmId, state]' --output text 2>/dev/null || true)
done
echo "  s3:       s3://$BUCKET (all objects)"
echo "  iam role: $BUILD_ROLE_NAME, $EXEC_ROLE_NAME (inline policy $POLICY_NAME)"

if [ "$MODE" = "--dry-run" ]; then
    echo "DRY RUN — nothing deleted."
    exit 0
fi

echo
for vm in "${VM_IDS[@]:-}"; do
    [ -z "$vm" ] && continue
    echo "Terminating microvm $vm"
    aws lambda-microvms terminate-microvm --region "$REGION" --microvm-identifier "$vm" >/dev/null 2>&1 \
        && echo "  terminated" || echo "  already gone or terminate failed (continuing)"
done

for arn in "${IMAGE_ARNS[@]}"; do
    name="${arn##*:}"           # e.g. coldstart-ping-microvm-500mb
    case "$name" in
        coldstart-ping-microvm-*) ;;
        *) echo "SKIP (not coldstart-ping-microvm-prefixed): $arn"; continue ;;
    esac
    echo "Deleting image $name"
    aws lambda-microvms delete-microvm-image --region "$REGION" --image-identifier "$arn" >/dev/null 2>&1 \
        && echo "  delete requested" || echo "  already gone or delete failed (continuing)"
done

echo "Deleting S3 bucket $BUCKET"
aws s3 rm "s3://$BUCKET" --recursive --region "$REGION" --only-show-errors 2>/dev/null || true
aws s3api delete-bucket --bucket "$BUCKET" --region "$REGION" 2>/dev/null \
    && echo "  deleted" || echo "  already gone or delete failed (continuing)"

for role in "$BUILD_ROLE_NAME" "$EXEC_ROLE_NAME"; do
    echo "Deleting IAM role $role"
    aws iam delete-role-policy --role-name "$role" --policy-name "$POLICY_NAME" 2>/dev/null \
        && echo "  inline policy deleted" || echo "  inline policy already gone"
    aws iam delete-role --role-name "$role" 2>/dev/null \
        && echo "  role deleted" || echo "  role already gone"
done

echo "Cleanup complete. deployments_microvm.json kept as a record (delete it manually if re-deploying fresh)."
