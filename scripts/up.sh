#!/usr/bin/env bash
# Start the local Proxmox VE (a Lima VM on an Apple Silicon Mac, nested KVM, Proxmox's official arm64 build), and on
# first run: install it, give the demo its own Proxmox user with only the rights the tools need, and write .env.
# The API token is generated inside Proxmox and written straight to .env (git-ignored); it is never printed.
set -euo pipefail
cd "$(dirname "$0")/.."
VM=pve

if ! limactl list -q 2> /dev/null | grep -qx "$VM"; then
  limactl create --name="$VM" --vm-type=vz --cpus=4 --memory=6 --disk=40 --set '.nestedVirtualization=true' \
    --set '.portForwards=[{"guestPort":8006,"hostIP":"127.0.0.1","hostPort":8006}]' --tty=false template:debian-13
fi
limactl start "$VM" --tty=false > /dev/null 2>&1 || true

if ! limactl shell "$VM" uname -r | grep -q -- '-pve$'; then
  limactl shell "$VM" sudo bash -s kernel < scripts/pve-install.sh
  limactl stop "$VM" > /dev/null && limactl start "$VM" --tty=false > /dev/null
fi
limactl shell "$VM" sudo bash -s pve < scripts/pve-install.sh

if [ ! -f .env ]; then
  # A role with only what the six tools use: read everything, power guests on and off, take snapshots.
  # (The token secret goes straight into .env below and is never printed.)
  # Proxmox lists a guest's backup files only to users who could back it up (VM.Backup on the guest and
  # Datastore.AllocateSpace on the backup storage), so those two are granted too, the second on `local` only.
  # No tool makes or deletes a backup.
  secret=$(limactl shell "$VM" sudo bash -c '
    pveum role list --output-format json | grep -q "\"ClaudeConnector\"" ||
      pveum role add ClaudeConnector --privs "VM.Audit,VM.Backup,VM.PowerMgmt,VM.Snapshot,Datastore.Audit,Sys.Audit"
    pveum role list --output-format json | grep -q "\"ClaudeBackupList\"" ||
      pveum role add ClaudeBackupList --privs "Datastore.AllocateSpace"
    pveum user list --output-format json | grep -q "claude@pve" ||
      pveum user add claude@pve --comment "Claude (Mojood Connect demo)"
    pveum acl modify / --users claude@pve --roles ClaudeConnector
    pveum acl modify /storage/local --users claude@pve --roles ClaudeBackupList
    pveum user token remove claude@pve connector > /dev/null 2>&1 || true
    pveum user token add claude@pve connector --privsep 0 --comment "MCP server" --output-format json' |
    python3 -c 'import json,sys; print(json.load(sys.stdin)["value"])')
  umask 077
  cat > .env << EOF
PVE_URL=https://127.0.0.1:8006
PVE_TOKEN_ID=claude@pve!connector
PVE_TOKEN_SECRET=$secret
PVE_VERIFY_TLS=0
PVE_SAMPLE=1
EOF
  unset secret
fi

# A read-only user for the web UI (PVEAuditor), for looking at the result in Proxmox itself. Its generated
# password goes only into .env.
if ! grep -q '^PVE_VIEWER_PASSWORD=' .env; then
  pw=$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')
  limactl shell "$VM" sudo bash -c 'pveum user list --output-format json | grep -q "viewer@pve" ||
    pveum user add viewer@pve --comment "Read-only web UI user (demo)"; pveum acl modify / --users viewer@pve --roles PVEAuditor'
  printf %s "$pw" | limactl shell "$VM" sudo bash -c 'pvesh set /access/password --userid viewer@pve --password "$(cat)"'
  printf 'PVE_VIEWER=viewer@pve\nPVE_VIEWER_PASSWORD=%s\n' "$pw" >> .env
  unset pw
fi
echo "Proxmox VE is up on https://127.0.0.1:8006 (web UI: sign in as root on the VM; API token in .env)"
