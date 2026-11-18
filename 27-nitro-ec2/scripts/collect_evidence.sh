#!/bin/bash
# Collect ONLY benchmark code/config/log/results; never /run credentials or homes.
set -euo pipefail
cd /opt/dsh-nitro-benchmark
mkdir -p results/host
uname -a > results/host/uname.txt
cat /proc/cmdline > results/host/cmdline.txt
lscpu > results/host/lscpu.txt
free -m > results/host/memory.txt
cat /sys/class/misc/nitro_enclaves/pcie_slots_in_use > results/host/slots-after.txt
nft -j list tables > results/host/nft-tables-after.json
ip -j link show > results/host/links-after.json
cat /proc/sys/net/ipv4/ip_forward > results/host/forwarding-after.txt
cp artifacts/manifest.json results/host/manifest.json
cp artifacts-src/dsh/package-lock.json results/host/package-lock.json
sha256sum benchmark.py guest/server.mjs guest/sandbox-init scripts/build_guest.sh > results/host/source-SHA256SUMS
# Write outside the directory being archived; otherwise tar detects its own
# output changing that directory even when the output filename is excluded.
test ! -e results/evidence-final.tgz
ARCHIVE=$(mktemp /opt/dsh-nitro-benchmark/evidence.XXXXXX.tgz)
tar --exclude='evidence*.tgz' -czf "$ARCHIVE" -C results .
mv "$ARCHIVE" results/evidence-final.tgz
sha256sum results/evidence-final.tgz
stat -c 'evidence_bytes=%s' results/evidence-final.tgz
