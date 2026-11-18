#!/bin/bash
set -euo pipefail
cd /opt/dsh-nitro-benchmark
ROOT=results/disk-isolation-20261117
mkdir -p "$ROOT/host"
uname -a > "$ROOT/host/uname.txt"
cat /proc/cmdline > "$ROOT/host/cmdline.txt"
cat /sys/class/misc/nitro_enclaves/pcie_slots_in_use > "$ROOT/host/slots-after.txt"
nft -j list tables > "$ROOT/host/nft-after.json"
ip -j link show > "$ROOT/host/links-after.json"
cp artifacts-disk-20261117/manifest.json "$ROOT/host/manifest.json"
sha256sum disk_isolation.py guest/disk-init guest/disk-probe.mjs scripts/build_disk_guest.sh > "$ROOT/host/source-SHA256SUMS"
# Do not archive the raw private disks or overwrite earlier cold-start evidence.
test ! -e "$ROOT/evidence.tgz"
ARCHIVE=$(mktemp /opt/dsh-nitro-benchmark/disk-evidence.XXXXXX.tgz)
tar -czf "$ARCHIVE" -C "$ROOT" .
mv "$ARCHIVE" "$ROOT/evidence.tgz"
sha256sum "$ROOT/evidence.tgz"
stat -c 'evidence_bytes=%s' "$ROOT/evidence.tgz"
