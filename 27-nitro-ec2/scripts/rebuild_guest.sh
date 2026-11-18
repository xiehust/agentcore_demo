#!/bin/bash
set -euo pipefail
cd /opt/dsh-nitro-benchmark
export PATH=/opt/node-v24.20.0-linux-arm64/bin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
# Preserve incomplete artifacts from the first failed build, do not overwrite them.
if [[ -d artifacts && ! -e artifacts/manifest.json ]]; then
    SAVED=false
    for N in {1..9}; do
        if [[ ! -e artifacts-failed-$N ]]; then
            mv artifacts "artifacts-failed-$N"
            SAVED=true
            break
        fi
    done
    $SAVED || { echo 'Too many failed build artifacts; stop' >&2; exit 1; }
fi
find artifacts-src/dsh/node_modules -name '*.node' -type f -print
DSH_DIR="$PWD/artifacts-src/dsh" NODE_BIN="$(command -v node)" bash scripts/build_guest.sh
cat artifacts/manifest.json
ls -lh artifacts/vmlinuz artifacts/initrd.img
free -m
