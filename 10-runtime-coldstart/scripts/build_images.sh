#!/usr/bin/env bash
# Build the three size-calibrated ARM64 ping-pong images:
#   coldstart-pingpong:500mb  (~500 MB uncompressed)
#   coldstart-pingpong:1gb    (~1024 MB)
#   coldstart-pingpong:2gb    (~1950 MB — AgentCore caps images at 2048 MB)
# Pad size is computed from the measured base-image size and written as
# /dev/urandom chunks of <=500MB, one layer per chunk (incompressible, so
# ECR compressed size ~= uncompressed size).
set -euo pipefail
cd "$(dirname "$0")/.."

IMAGE=coldstart-pingpong
PLATFORM=linux/arm64

# Targets and tolerances (MB, uncompressed docker size)
declare -A TARGET=( [500mb]=500 [1gb]=1024 [2gb]=1950 )
declare -A MIN_MB=( [500mb]=475 [1gb]=975  [2gb]=1850 )
declare -A MAX_MB=( [500mb]=525 [1gb]=1075 [2gb]=2040 )

echo "== Building unpadded base to measure its size =="
docker build --platform "$PLATFORM" -f docker/Dockerfile -t "$IMAGE:base" . >/dev/null
BASE_BYTES=$(docker image inspect --format '{{.Size}}' "$IMAGE:base")
BASE_MB=$(( BASE_BYTES / 1024 / 1024 ))
echo "Base image: ${BASE_MB} MB (${BASE_BYTES} bytes)"

build_variant() {
    local tag="$1" target_mb="$2"
    local pad_total=$(( target_mb - BASE_MB ))
    if (( pad_total <= 0 )); then
        echo "ERROR: target ${target_mb}MB <= base ${BASE_MB}MB" >&2
        exit 1
    fi
    local args=() rem=$pad_total chunk i
    for i in 1 2 3 4; do
        chunk=$(( rem >= 500 ? 500 : rem ))
        (( chunk < 0 )) && chunk=0
        args+=(--build-arg "PAD${i}_MB=${chunk}")
        rem=$(( rem - chunk ))
    done
    if (( rem != 0 )); then
        echo "ERROR: pad ${pad_total}MB exceeds 4x500MB chunk capacity" >&2
        exit 1
    fi
    echo "== Building $IMAGE:$tag (pad ${pad_total}MB: ${args[*]}) =="
    docker build --platform "$PLATFORM" -f docker/Dockerfile "${args[@]}" -t "$IMAGE:$tag" . >/dev/null
}

for tag in 500mb 1gb 2gb; do
    build_variant "$tag" "${TARGET[$tag]}"
done

echo
echo "== Size verification =="
printf "%-8s %14s %8s %6s %12s\n" TAG BYTES MB ARCH "ALLOWED(MB)"
fail=0
for tag in 500mb 1gb 2gb; do
    bytes=$(docker image inspect --format '{{.Size}}' "$IMAGE:$tag")
    arch=$(docker image inspect --format '{{.Architecture}}' "$IMAGE:$tag")
    mb=$(( bytes / 1024 / 1024 ))
    printf "%-8s %14d %8d %6s %6d-%d\n" "$tag" "$bytes" "$mb" "$arch" "${MIN_MB[$tag]}" "${MAX_MB[$tag]}"
    if (( mb < MIN_MB[$tag] || mb > MAX_MB[$tag] )); then
        echo "FAIL: $tag out of tolerance"
        fail=1
    fi
    if [ "$arch" != "arm64" ]; then
        echo "FAIL: $tag architecture is $arch, expected arm64"
        fail=1
    fi
done
if (( fail )); then
    exit 1
fi
echo "All images built and within tolerance."
