#!/usr/bin/env bash
# Build the ARM64 DSH agent image (AgentCore Runtime only runs linux/arm64).
set -euo pipefail
cd "$(dirname "$0")/.."

DSH_VERSION="${DSH_VERSION:-0.2.2-rc.1}"
IMAGE="${IMAGE:-dsh-coldstart}"
TAG="${TAG:-dsh-${DSH_VERSION}}"

docker buildx build --platform linux/arm64 --load \
    --build-arg "DSH_VERSION=${DSH_VERSION}" \
    --build-arg "IMAGE_TAG=${TAG}" \
    -f docker/Dockerfile -t "${IMAGE}:${TAG}" .

arch=$(docker image inspect --format '{{.Architecture}}' "${IMAGE}:${TAG}")
size=$(docker image inspect --format '{{.Size}}' "${IMAGE}:${TAG}")
echo "Built ${IMAGE}:${TAG}  arch=${arch}  size=$((size / 1024 / 1024)) MB"
[ "$arch" = "arm64" ] || { echo "FATAL: image is ${arch}, AgentCore needs arm64" >&2; exit 1; }
