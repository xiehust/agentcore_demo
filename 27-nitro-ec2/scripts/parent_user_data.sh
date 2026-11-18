#!/bin/bash
# cloud-init runs this once on the NEW dedicated benchmark parent only.
set -euo pipefail
uname -r
if ! grep -q 'nitro_enclaves.mempool=8G@0' /proc/cmdline; then
    grubby --update-kernel=ALL --args='nitro_enclaves.mempool=8G@0'
    echo 'Reserved Nitro memory pool configured; rebooting before guest preparation'
    reboot
fi
