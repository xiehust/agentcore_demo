#!/bin/bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
REGION=$(python3 -c 'import json;print(json.load(open("state.json"))["region"])')
TAG=${1:-dsh-$(date -u +%Y%m%dT%H%M%SZ)}
IMAGE=$(python3 -c 'import json;print(json.load(open("state.json"))["image_tag_uri"].rsplit(":",1)[0])'):$TAG
REGISTRY=${IMAGE%%/*}
export DOCKER_CONFIG=$(mktemp -d)
trap 'rm -rf "$DOCKER_CONFIG"' EXIT
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REGISTRY"
docker build --platform linux/arm64 -t "$IMAGE" .
docker push "$IMAGE"
python3 deploy.py register --image-tag "$TAG"
