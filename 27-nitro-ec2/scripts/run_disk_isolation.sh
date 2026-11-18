#!/bin/bash
set -euo pipefail
cd /opt/dsh-nitro-benchmark
modprobe nitro_enclaves
cat /proc/cmdline
cat /sys/class/misc/nitro_enclaves/mem_pool/free
# Install only the documented filesystem tooling on the dedicated test parent.
dnf install -y e2fsprogs
export PATH=/opt/node-v24.20.0-linux-arm64/bin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
OUTPUT_DIR="$PWD/artifacts-disk-20261117" DSH_DIR="$PWD/artifacts-src/dsh" \
  NODE_BIN="$(command -v node)" bash scripts/build_disk_guest.sh
python3 disk_isolation.py --kernel artifacts-disk-20261117/vmlinuz \
  --initrd artifacts-disk-20261117/initrd.img --manifest artifacts-disk-20261117/manifest.json \
  --direct-mode --out results/disk-isolation-20261117/direct
python3 disk_isolation.py --kernel artifacts-disk-20261117/vmlinuz \
  --initrd artifacts-disk-20261117/initrd.img --manifest artifacts-disk-20261117/manifest.json \
  --out results/disk-isolation-20261117/confidential
