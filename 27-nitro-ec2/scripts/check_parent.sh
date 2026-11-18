#!/bin/bash
set -euo pipefail
modprobe nitro_enclaves
uname -r
cat /proc/cmdline
ls -l /dev/nitro_enclaves
cat /sys/class/misc/nitro_enclaves/pcie_slots
cat /sys/class/misc/nitro_enclaves/pcie_slots_in_use
cat /sys/class/misc/nitro_enclaves/mem_pool/free
dmesg | grep -E 'nitro_enclaves:.*(pool|reserved)' || true
