#!/bin/bash
set -euo pipefail
cd /opt/dsh-nitro-benchmark
cat results/agent-direct/round-0002/records.json
cat results/agent-direct/round-0002/firecracker.log
cat /sys/class/misc/nitro_enclaves/pcie_slots_in_use
