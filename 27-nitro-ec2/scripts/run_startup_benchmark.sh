#!/bin/bash
set -euo pipefail
cd /opt/dsh-nitro-benchmark
python3 benchmark.py --rounds 20 --out results/startup-confidential
python3 benchmark.py --rounds 20 --direct-mode --out results/startup-direct
