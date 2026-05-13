#!/bin/bash

set -euxo pipefail

PORT=8083
SCRIPT_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/maas-offline-http.py"
SCRIPT_DST_DIR=/opt/maas-offline
SCRIPT_DST="${SCRIPT_DST_DIR}/maas-offline-http.py"
UNIT_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/systemd/maas-offline-http.service"
UNIT_DST=/etc/systemd/system/maas-offline-http.service

sudo mkdir -p "$SCRIPT_DST_DIR"
sudo cp "$SCRIPT_SRC" "$SCRIPT_DST"
sudo chmod +x "$SCRIPT_DST"
sudo cp "$UNIT_SRC" "$UNIT_DST"

sudo pkill -f "python3 -m http.server 8081" || true
sudo pkill -f "python3 -m http.server 8082" || true
sudo pkill -f "python3 -m http.server 8083" || true
sudo pkill -f "python3 -m http.server ${PORT}" || true

sudo systemctl daemon-reload
sudo systemctl enable --now maas-offline-http.service

sudo systemctl status maas-offline-http.service --no-pager

