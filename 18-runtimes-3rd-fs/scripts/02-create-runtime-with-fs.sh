#!/usr/bin/env bash
# Attach native file systems to an AgentCore Runtime (microVM):
#   sessionStorage (no VPC)  +  EFS access point  +  S3 Files access point (both need VPC mode).
#
# Prereqs (see docs/01-filesystem-fuse.md):
#   * awscli recent enough to know efsAccessPoint / s3FilesAccessPoint (aws --version; update if ValidationException)
#   * execution role: elasticfilesystem:ClientMount/ClientWrite, s3files:ClientMount/ClientWrite/GetAccessPoint
#   * runtime SG -> mount-target SG TCP 2049; mount targets in the runtime subnets' AZs
#
# Usage:
#   REGION=us-east-2 RUNTIME_ID=<id> SUBNETS=subnet-a,subnet-b SG=sg-xxx \
#   EFS_AP_ARN=arn:aws:elasticfilesystem:...:access-point/fsap-... \
#   S3FILES_AP_ARN=arn:aws:s3files:...:file-system/.../access-point/... \
#   ./scripts/02-create-runtime-with-fs.sh
#
# Omit EFS_AP_ARN / S3FILES_AP_ARN to attach only sessionStorage (PUBLIC network is then fine).
set -euo pipefail

: "${REGION:?REGION required}"
: "${RUNTIME_ID:?RUNTIME_ID required (existing agent runtime id)}"

FS_CONFIG='[{"sessionStorage":{"mountPath":"/mnt/workspace"}}'
if [[ -n "${EFS_AP_ARN:-}" ]]; then
  FS_CONFIG+=",{\"efsAccessPoint\":{\"accessPointArn\":\"${EFS_AP_ARN}\",\"mountPath\":\"/mnt/shared\"}}"
fi
if [[ -n "${S3FILES_AP_ARN:-}" ]]; then
  FS_CONFIG+=",{\"s3FilesAccessPoint\":{\"accessPointArn\":\"${S3FILES_AP_ARN}\",\"mountPath\":\"/mnt/datasets\"}}"
fi
FS_CONFIG+=']'

# UpdateAgentRuntime requires the full artifact/role again; read them from the current definition.
CURRENT=$(aws bedrock-agentcore-control get-agent-runtime --region "$REGION" --agent-runtime-id "$RUNTIME_ID")
ARTIFACT=$(jq -c '.agentRuntimeArtifact' <<<"$CURRENT")
ROLE_ARN=$(jq -r '.roleArn' <<<"$CURRENT")
PROTOCOL=$(jq -c '.protocolConfiguration // {"serverProtocol":"HTTP"}' <<<"$CURRENT")

if [[ -n "${EFS_AP_ARN:-}${S3FILES_AP_ARN:-}" ]]; then
  : "${SUBNETS:?SUBNETS required for VPC mode (comma-separated)}"
  : "${SG:?SG required for VPC mode}"
  SUBNET_JSON=$(jq -cn --arg s "$SUBNETS" '$s | split(",")')
  NETWORK="{\"networkMode\":\"VPC\",\"networkModeConfig\":{\"subnets\":${SUBNET_JSON},\"securityGroups\":[\"${SG}\"]}}"
else
  NETWORK=$(jq -c '.networkConfiguration' <<<"$CURRENT")
fi

echo "filesystemConfigurations: $FS_CONFIG"
aws bedrock-agentcore-control update-agent-runtime \
  --region "$REGION" \
  --agent-runtime-id "$RUNTIME_ID" \
  --agent-runtime-artifact "$ARTIFACT" \
  --role-arn "$ROLE_ARN" \
  --protocol-configuration "$PROTOCOL" \
  --network-configuration "$NETWORK" \
  --filesystem-configurations "$FS_CONFIG" \
  --query '{version:agentRuntimeVersion,status:status}' --output table

cat <<'EOF'

Verify from inside a session (same probe client as 01-fuse-probe.py):
  python3 - <<'PY'
  import sys; sys.path.insert(0, "scripts")
  from runtime_cmd import RuntimeSession
  with RuntimeSession("<runtime-arn>", "<region>") as s:
      s.warm_up()
      print(s.run_script("df -hP /mnt/* ; mount | grep /mnt")["stdout"])
  PY
EOF
