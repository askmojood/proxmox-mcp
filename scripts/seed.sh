#!/usr/bin/env bash
# Runs as root inside the Lima VM (scripts/reset.sh sends it): removes any guests, then builds the SAMPLE estate of
# the made-up Harbor Trading: six VMs (Alpine, really booting under KVM) and four LXC containers, a nightly backup
# job that misses some of them, real vzdump backups, and snapshots. Proxmox stamps backups and snapshots with the
# time they were made, so the last step moves a few of those stamps back (backup file names, snapshot times) to
# give the estate a history. Everything here is SAMPLE.
set -euo pipefail
node=$(hostname)
img=/var/lib/vz/template/alpine-aarch64.qcow2
tmpl=alpine-3.24-default_20260803_arm64.tar.xz

for id in $(qm list 2> /dev/null | awk 'NR>1 {print $1}'); do qm stop "$id" --skiplock 1 > /dev/null 2>&1 || true; qm destroy "$id" --purge 1 > /dev/null; done
for id in $(pct list 2> /dev/null | awk 'NR>1 {print $1}'); do pct stop "$id" > /dev/null 2>&1 || true; pct destroy "$id" --purge 1 > /dev/null; done
pvesh delete /cluster/backup/harbor-nightly > /dev/null 2>&1 || true
rm -f /var/lib/vz/dump/vzdump-*

[ -f "$img" ] || wget -q -O "$img" https://dl-cdn.alpinelinux.org/alpine/v3.22/releases/cloud/nocloud_alpine-3.22.4-aarch64-uefi-cloudinit-r0.qcow2
[ -f "/var/lib/vz/template/cache/$tmpl" ] || pveam download local "$tmpl" > /dev/null

vm() { # id name memory tags description
  qm create "$1" --name "$2" --memory "$3" --cores 1 --bios ovmf --efidisk0 tank:1,efitype=4m --scsihw virtio-scsi-pci \
    --net0 virtio,bridge=vmbr0 --ostype l26 --serial0 socket --vga serial0 --tags "$4" --description "$5" --onboot 1 > /dev/null
  qm set "$1" --scsi0 "tank:0,import-from=$img" --boot order=scsi0 > /dev/null
}
ct() { # id hostname memory tags description
  pct create "$1" "local:vztmpl/$tmpl" --hostname "$2" --memory "$3" --cores 1 --rootfs tank:1 --unprivileged 1 \
    --net0 name=eth0,bridge=vmbr0,ip=dhcp --tags "$4" --description "$5" --onboot 1 > /dev/null
}

vm 101 erp-db 512 "prod;erp" "SAMPLE. ERP database (PostgreSQL). Owner: Hana Saleh, IT."
vm 102 erp-app 384 "prod;erp" "SAMPLE. ERP application server."
vm 103 file-server 384 "prod" "SAMPLE. Shared drives for Finance and Sales."
vm 104 mail-relay 384 "prod" "SAMPLE. Outgoing mail relay for the ERP."
vm 105 crm-old 384 "retired" "SAMPLE. Old CRM, replaced in July. Keep until the archive export is checked."
vm 106 staging-web 384 "staging" "SAMPLE. Staging copy of the website."
ct 201 web-01 128 "prod;web" "SAMPLE. Website, node 1."
ct 202 web-02 128 "prod;web" "SAMPLE. Website, node 2."
ct 203 monitoring 128 "ops" "SAMPLE. Uptime checks and alerts."
ct 204 vpn-gw 128 "prod" "SAMPLE. VPN gateway for remote staff."
qm set 104 --onboot 0 > /dev/null   # stopped after a failed update and never started again

# Nightly backup job that covers most, not all: file-server, mail-relay, crm-old and vpn-gw are in no job.
pvesh create /cluster/backup --id harbor-nightly --schedule "01:00" --storage local --mode snapshot \
  --vmid 101,102,106,201,202,203 --compress zstd --enabled 1 --comment "SAMPLE nightly backup" > /dev/null

# VMs get 384 MB or more: below that, QEMU's own overhead makes a VM look over its memory.
# Real backups (taken while stopped, quick), then start what should be running.
for id in 101 102 106 201 202 203; do vzdump "$id" --storage local --mode stop --compress zstd --quiet 1 > /dev/null 2>&1; done
for id in 101 102 103 106; do qm start "$id" > /dev/null; done
for id in 201 202 203 204; do pct start "$id" > /dev/null; done

# Snapshots: a recent one before an ERP upgrade, and one left behind on crm-old.
qm snapshot 101 before-upgrade --description "SAMPLE. Before the ERP 14.2 upgrade" > /dev/null
qm snapshot 105 pre-migration --description "SAMPLE. Before moving contacts to the new CRM" > /dev/null

# History: erp-app's last backup is 9 days old (its backup failed since), the rest are from last night;
# crm-old's snapshot is 96 days old, erp-db's 3 days.
stamp() { date -u -d "$1" +%Y_%m_%d-%H_%M_%S; }
for f in /var/lib/vz/dump/vzdump-*; do
  id=$(basename "$f" | cut -d- -f3)
  when=$([ "$id" = 102 ] && stamp "9 days ago 01:00" || stamp "today 01:00")
  new=$(basename "$f" | sed -E "s/[0-9]{4}_[0-9]{2}_[0-9]{2}-[0-9]{2}_[0-9]{2}_[0-9]{2}/$when/")
  mv "$f" "/var/lib/vz/dump/$new"
done
setsnap() { sed -i -E "/^\[$2\]/,/^\[/ s/^snaptime: .*/snaptime: $(date -d "$3" +%s)/" "/etc/pve/qemu-server/$1.conf"; }
setsnap 101 before-upgrade "3 days ago 22:00"
setsnap 105 pre-migration "96 days ago 18:30"
# The setup itself (creating, first backups, starts) is not part of the story: start the task log empty.
: > /var/log/pve/tasks/index; : > /var/log/pve/tasks/active
# Proxmox refreshes guest status every few seconds: wait until it reports the eight running guests.
for _ in $(seq 30); do
  [ "$(pvesh get /cluster/resources --type vm --output-format json | grep -o '"status":"running"' | wc -l)" -ge 8 ] && break
  sleep 2
done
echo "SAMPLE estate ready on $node: $(qm list | awk 'NR>1' | wc -l) VMs, $(pct list | awk 'NR>1' | wc -l) containers"
