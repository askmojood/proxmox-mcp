#!/usr/bin/env bash
# Runs as root inside the Lima VM (scripts/up.sh sends it): installs Proxmox VE 9 on Debian 13 the way Proxmox's
# wiki describes it (https://pve.proxmox.com/wiki/Install_Proxmox_VE_on_Debian_13_Trixie), arm64 build. Safe to run
# again: each step checks first. Reboots into the Proxmox kernel are done by up.sh between the two stages.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
stage="${1:-all}"

ip4=$(ip -4 -o addr show eth0 | awk '{print $4}' | cut -d/ -f1)
grep -q "^$ip4 lima-pve" /etc/hosts || { sed -i '/lima-pve/d' /etc/hosts; echo "$ip4 lima-pve.local lima-pve" >> /etc/hosts; }

if [ "$stage" = kernel ] || [ "$stage" = all ]; then
  key=/usr/share/keyrings/proxmox-archive-keyring.gpg
  [ -f "$key" ] || wget -q https://enterprise.proxmox.com/debian/proxmox-archive-keyring-trixie.gpg -O "$key"
  echo "136673be77aba35dcce385b28737689ad64fd785a797e57897589aed08db6e45  $key" | sha256sum -c --quiet
  printf 'Types: deb\nURIs: http://download.proxmox.com/debian/pve\nSuites: trixie\nComponents: pve-no-subscription\nSigned-By: %s\n' \
    "$key" > /etc/apt/sources.list.d/pve-install-repo.sources
  apt-get update -qq
  apt-get -y -qq full-upgrade > /dev/null
  apt-get -y -qq install proxmox-default-kernel > /dev/null
fi

if [ "$stage" = pve ] || [ "$stage" = all ]; then
  if ! command -v pveversion > /dev/null; then
    echo "postfix postfix/main_mailer_type select Local only" | debconf-set-selections
    echo "postfix postfix/mailname string lima-pve.local" | debconf-set-selections
    apt-get -y -qq install proxmox-ve postfix open-iscsi chrony > /tmp/pve-install.log 2>&1 || { tail -30 /tmp/pve-install.log; exit 1; }
  fi
  # Storage that can snapshot both VMs and containers: a ZFS pool on a file (fine for a demo, not for production).
  modprobe zfs
  [ -f /var/lib/tank.img ] || truncate -s 14G /var/lib/tank.img
  zpool list tank > /dev/null 2>&1 || zpool import -d /var/lib tank 2> /dev/null || zpool create -o ashift=12 tank /var/lib/tank.img
  pvesm status | grep -q '^tank ' || pvesm add zfspool tank --pool tank --content images,rootdir --sparse 1
  # A bridge for the guests, with no uplink: they don't need the network for the demo, and Lima keeps its own eth0.
  grep -q vmbr0 /etc/network/interfaces || printf '\nauto vmbr0\niface vmbr0 inet static\n    address 10.10.10.1/24\n    bridge-ports none\n    bridge-stp off\n    bridge-fd 0\n' >> /etc/network/interfaces
  ip link show vmbr0 > /dev/null 2>&1 || { ip link add vmbr0 type bridge; ip addr add 10.10.10.1/24 dev vmbr0; ip link set vmbr0 up; }
  pveversion
fi
