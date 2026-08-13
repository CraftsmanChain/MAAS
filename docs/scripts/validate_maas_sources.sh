#!/bin/bash

set -euo pipefail

SOURCES_DIR="${1:?usage: validate_maas_sources.sh /path/to/MAAS-sources}"
SERIES="${SERIES:-jammy}"

missing=0
warning=0

check_path() {
  local path="$1"
  if [ -e "$path" ]; then
    printf '[ok] %s\n' "$path"
  else
    printf '[missing] %s\n' "$path"
    missing=1
  fi
}

check_path "${SOURCES_DIR}/mirror/ephemeral-v3/stable/streams/v1/index.sjson"
check_path "${SOURCES_DIR}/iso/dists/${SERIES}/Release"
check_path "${SOURCES_DIR}/tools/lldpd-mini-repo/dists/${SERIES}/Release"
check_path "${SOURCES_DIR}/tools/maas-control-repo/dists/${SERIES}/main/binary-amd64/Packages.gz"
check_path "${SOURCES_DIR}/stage1/inventory.csv"
check_path "${SOURCES_DIR}/stage1/defaults.yaml"
check_path "${SOURCES_DIR}/stage1/state.json"
check_path "${SOURCES_DIR}/ansible/runtime/debs/SHA256SUMS"

ansible_deb_dir="${SOURCES_DIR}/ansible/runtime/debs"
ansible_debs="$(find "$ansible_deb_dir" -maxdepth 1 -type f -name '*.deb' 2>/dev/null | wc -l | tr -d ' ')"
if [ "${ansible_debs:-0}" -ge 4 ]; then
  echo "[ok] Ansible offline deb packages found: ${ansible_debs}"
  if command -v sha256sum >/dev/null 2>&1; then
    (cd "$ansible_deb_dir" && sha256sum -c SHA256SUMS)
  fi
else
  echo "[missing] Ansible offline deb packages incomplete: expected at least 4, found ${ansible_debs:-0}"
  missing=1
fi

maas_debs="$(
  find "$SOURCES_DIR" -type f \
    \( -name 'maas-region-api*.deb' -o -name 'maas-rack-controller*.deb' -o -name 'maas-cli*.deb' \) \
    2>/dev/null | wc -l | tr -d ' '
)"

if [ "${maas_debs:-0}" -gt 0 ]; then
  echo "[ok] MAAS control-plane deb packages found: ${maas_debs}"
else
  echo "[warn] MAAS control-plane deb packages not found: maas-region-api / maas-rack-controller / maas-cli"
  warning=1
fi

dnsutils_debs="$(
  find "$SOURCES_DIR/tools/maas-control-repo" -type f \
    \( -name 'dnsutils*.deb' -o -name 'bind9-dnsutils*.deb' \) \
    2>/dev/null | wc -l | tr -d ' '
)"

if [ "${dnsutils_debs:-0}" -gt 0 ]; then
  echo "[ok] MAAS control-plane DNS utility deb packages found: ${dnsutils_debs}"
else
  echo "[missing] MAAS control-plane DNS utilities not found: dnsutils/bind9-dnsutils provide nsupdate"
  missing=1
fi

if [ "$missing" -ne 0 ]; then
  echo "maas_sources_valid=false"
  exit 1
fi

echo "maas_sources_valid=true"
if [ "$warning" -ne 0 ]; then
  echo "maas_control_plane_install_resources=false"
else
  echo "maas_control_plane_install_resources=true"
fi
