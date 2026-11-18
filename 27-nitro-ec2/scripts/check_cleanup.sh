#!/bin/bash
set -euo pipefail
cd /opt/dsh-nitro-benchmark
SLOTS=$(cat /sys/class/misc/nitro_enclaves/pcie_slots_in_use)
[[ $SLOTS == 0 ]] || { echo 'Sandbox leak detected'; exit 1; }
if find /run -maxdepth 1 -name 'dsh-nitro-creds.*' -print -quit | grep -q .; then
    echo 'Credential temp file remains'; exit 1
fi
if nft -j list tables | grep -q 'dshb_'; then
    echo 'Benchmark nft table remains'; exit 1
fi
if ip -j link show | grep -Eq '"ifname":[[:space:]]*"dsh'; then
    echo 'Benchmark TAP remains'; exit 1
fi
echo 'PASS: no running Sandbox, credential temp file, benchmark nft table or TAP'
cat /proc/sys/net/ipv4/ip_forward
