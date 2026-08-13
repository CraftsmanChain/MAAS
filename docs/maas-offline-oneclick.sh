#!/bin/bash

set -euxo pipefail

PORT=8083
OFFLINE_ROOT=/srv/maas-offline
MIRROR_DIR="${OFFLINE_ROOT}/mirror"
ISO_DIR="${OFFLINE_ROOT}/iso"
TOOLS_DIR="${OFFLINE_ROOT}/tools"
DISKLESS_DIR="${OFFLINE_ROOT}/diskless"
STAGE1_DIR="${OFFLINE_ROOT}/stage1"
LLDPD_REPO_DIR="${TOOLS_DIR}/lldpd-mini-repo"
LEGACY_MIRROR_DIR=/srv/maas-mirror
LEGACY_ISO_DIR=/root/ubuntu22.04.4
LEGACY_TOOLS_DIR=/root/tools
LEGACY_LLDPD_REPO_DIR=/srv/lldpd-mini-repo
SCRIPT_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/maas-offline-http.py"
SCRIPT_DST_DIR=/opt/maas-offline
SCRIPT_DST="${SCRIPT_DST_DIR}/maas-offline-http.py"
UNIT_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/systemd/maas-offline-http.service"
UNIT_DST=/etc/systemd/system/maas-offline-http.service

sync_dir_if_exists() {
  local src="$1"
  local dst="$2"
  if [ -d "$src" ]; then
    sudo rsync -a "$src"/ "$dst"/
  fi
}

repair_lowlatency_boot_artifacts() {
  local generic_dir=""
  local base=""
  local lowlatency_dir=""

  if [ ! -d "${MIRROR_DIR}/ephemeral-v3/stable" ]; then
    return 0
  fi

  while IFS= read -r generic_dir; do
    base="$(dirname "$generic_dir")"
    lowlatency_dir="${base}/lowlatency"
    sudo mkdir -p "$lowlatency_dir"

    if [ -f "${generic_dir}/boot-kernel" ] && [ ! -f "${lowlatency_dir}/boot-kernel" ]; then
      sudo cp -a "${generic_dir}/boot-kernel" "${lowlatency_dir}/boot-kernel"
    fi
    if [ -f "${generic_dir}/boot-initrd" ] && [ ! -f "${lowlatency_dir}/boot-initrd" ]; then
      sudo cp -a "${generic_dir}/boot-initrd" "${lowlatency_dir}/boot-initrd"
    fi
  done < <(find "${MIRROR_DIR}/ephemeral-v3/stable" -type d -path '*/ga-22.04/generic' | sort)
}

sudo mkdir -p "$SCRIPT_DST_DIR"
sudo mkdir -p "$MIRROR_DIR" "$ISO_DIR" "$TOOLS_DIR" "$DISKLESS_DIR" "$STAGE1_DIR" "$LLDPD_REPO_DIR"
sync_dir_if_exists "$LEGACY_MIRROR_DIR" "$MIRROR_DIR"
sync_dir_if_exists "$LEGACY_ISO_DIR" "$ISO_DIR"
sync_dir_if_exists "$LEGACY_TOOLS_DIR" "$TOOLS_DIR"
sync_dir_if_exists "$LEGACY_LLDPD_REPO_DIR" "$LLDPD_REPO_DIR"
repair_lowlatency_boot_artifacts

sudo cp "$SCRIPT_SRC" "$SCRIPT_DST"
sudo chmod +x "$SCRIPT_DST"
sudo cp "$UNIT_SRC" "$UNIT_DST"

sudo pkill -f "python3 -m http.server 8081" || true
sudo pkill -f "python3 -m http.server 8082" || true
sudo pkill -f "python3 -m http.server 8083" || true
sudo pkill -f "python3 -m http.server 8899" || true
sudo pkill -f "/opt/maas-offline/maas-offline-http.py --bind 0.0.0.0 --port ${PORT}" || true
sudo systemctl disable --now maas-ubuntu-apt-http.service 2>/dev/null || true
sudo systemctl disable --now lldpd-mini-repo.service 2>/dev/null || true

sudo systemctl daemon-reload
sudo systemctl enable --now maas-offline-http.service

sudo systemctl status maas-offline-http.service --no-pager

cat <<EOF
offline_root=${OFFLINE_ROOT}
mirror_dir=${MIRROR_DIR}
iso_dir=${ISO_DIR}
tools_dir=${TOOLS_DIR}
urls:
  http://<server-ip>:${PORT}/mirror/
  http://<server-ip>:${PORT}/iso/
  http://<server-ip>:${PORT}/tools/
  http://<server-ip>:${PORT}/diskless/
  http://<server-ip>:${PORT}/stage1/
  http://<server-ip>:${PORT}/tools/lldpd-mini-repo/

maas_package_repo_examples:
  maas admin package-repository update 1 url=http://<server-ip>:${PORT}/iso
  maas admin package-repository update 3 url=http://<server-ip>:${PORT}/tools/lldpd-mini-repo
EOF
