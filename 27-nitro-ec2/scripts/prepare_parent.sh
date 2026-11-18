#!/bin/bash
# Run on the dedicated parent through SSM. No credentials or secret outputs.
set -euo pipefail
cd /opt/dsh-nitro-benchmark

uname -r
[[ $(uname -r) == 6.18.*amzn2023* ]] || { echo 'Wrong kernel; stop' >&2; exit 1; }
test -e /dev/nitro_enclaves
cat /proc/cmdline
cat /sys/class/misc/nitro_enclaves/pcie_slots
# Installation belongs to the requested dedicated deployment, not the workstation.
dnf install -y dracut nftables iproute kmod git tar gzip xz
VERSION=1.18.0
ARCH=$(uname -m)
curl --connect-timeout 15 --max-time 180 -fsSL -o /tmp/dsh-firecracker.tgz \
  "https://github.com/firecracker-microvm/firecracker/releases/download/v${VERSION}/firecracker-v${VERSION}-${ARCH}.tgz"
tar -xzf /tmp/dsh-firecracker.tgz -C /tmp
install -m 755 "/tmp/release-v${VERSION}-${ARCH}/firecracker-v${VERSION}-${ARCH}" /usr/local/bin/firecracker
firecracker --version
NODE_VERSION=v24.20.0
NODE_ARCH=arm64
[[ $ARCH == aarch64 ]] || NODE_ARCH=x64
curl --connect-timeout 15 --max-time 180 -fsSL -o /tmp/dsh-node.tar.xz \
  "https://nodejs.org/dist/${NODE_VERSION}/node-${NODE_VERSION}-linux-${NODE_ARCH}.tar.xz"
curl --connect-timeout 15 --max-time 60 -fsSL -o /tmp/dsh-node-SHASUMS256.txt \
  "https://nodejs.org/dist/${NODE_VERSION}/SHASUMS256.txt"
EXPECTED=$(awk -v name="node-${NODE_VERSION}-linux-${NODE_ARCH}.tar.xz" '$2 == name {print $1}' /tmp/dsh-node-SHASUMS256.txt)
[[ -n $EXPECTED ]] || { echo 'Node checksum not found; stop' >&2; exit 1; }
echo "$EXPECTED  /tmp/dsh-node.tar.xz" | sha256sum -c -
tar -xJf /tmp/dsh-node.tar.xz -C /opt
export PATH="/opt/node-${NODE_VERSION}-linux-${NODE_ARCH}/bin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
node --version
npm --version
mkdir -p artifacts-src/dsh
cd artifacts-src/dsh
npm init -y >/dev/null
npm install --omit=dev --no-audit --no-fund '@deepseek-ai/dsh@0.2.2-rc.1'
cd ../..

DSH_DIR="$PWD/artifacts-src/dsh" NODE_BIN="$(command -v node)" bash scripts/build_guest.sh
cat artifacts/manifest.json
ls -lh artifacts/vmlinuz artifacts/initrd.img
free -m
cat /sys/class/misc/nitro_enclaves/mem_pool/free
