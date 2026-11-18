#!/bin/bash
set -uo pipefail
cd /opt/dsh-nitro-benchmark
python3 benchmark.py --rounds 1 --debug --boot-timeout 120 --out results/smoke-confidential
RC=$?
if [[ $RC != 0 ]]; then
    find results/smoke-confidential -name firecracker.log -exec tail -120 {} \;
fi
exit "$RC"
