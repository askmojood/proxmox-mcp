#!/usr/bin/env bash
# Start over: remove every guest and build the SAMPLE Harbor Trading estate again (the Proxmox install is kept).
set -euo pipefail
cd "$(dirname "$0")/.."
./scripts/up.sh
limactl shell pve sudo bash -s < scripts/seed.sh
