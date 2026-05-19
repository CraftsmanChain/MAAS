#!/bin/bash

set -euxo pipefail

PORT=8083
OFFLINE_ROOT=/srv/maas-offline
MIRROR_DIR="${OFFLINE_ROOT}/mirror"
ISO_DIR="${OFFLINE_ROOT}/iso"
TOOLS_DIR="${OFFLINE_ROOT}/tools"
SCRIPT_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/maas-offline-http.py"
SCRIPT_DST_DIR=/opt/maas-offline
SCRIPT_DST="${SCRIPT_DST_DIR}/maas-offline-http.py"
UNIT_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/systemd/maas-offline-http.service"
UNIT_DST=/etc/systemd/system/maas-offline-http.service

sudo mkdir -p "$SCRIPT_DST_DIR"
sudo mkdir -p "$MIRROR_DIR" "$ISO_DIR" "$TOOLS_DIR"
sudo cp "$SCRIPT_SRC" "$SCRIPT_DST"
sudo chmod +x "$SCRIPT_DST"
sudo cp "$UNIT_SRC" "$UNIT_DST"

sudo pkill -f "python3 -m http.server 8083" || true
sudo pkill -f "/opt/maas-offline/maas-offline-http.py --bind 0.0.0.0 --port ${PORT}" || true

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
EOF
