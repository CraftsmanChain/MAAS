#!/bin/bash

set -euo pipefail

DRY_RUN=0
ENABLE_DHCP=0
ENABLE_COLLECTOR=1
ENABLE_HTTP=1
OFFLINE_ROOT=/srv/maas-offline
SERVER_IP=""
HTTP_PORT=8083
STAGE1_PORT=8091
STAGE1_ISO_NAME=""
DHCP_INTERFACE=""
DHCP_RANGE=""
DHCP_ROUTER=""
DHCP_DNS=""
UEFI_IPXE_SOURCE="ipxe.efi"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<'EOF'
Usage: diskless-stage1-oneclick.sh [options]

Prepare and optionally start Stage1 diskless services.

Options:
  --server-ip IP              Server IP advertised in iPXE and collector URLs.
  --offline-root DIR          Offline root, default /srv/maas-offline.
  --http-port PORT            Offline HTTP port, default 8083.
  --stage1-port PORT          Stage1 collector port, default 8091.
  --stage1-iso-name NAME      Ubuntu 22.04 live-server ISO under OFFLINE_ROOT/iso.
  --enable-dhcp               Install and start diskless dnsmasq DHCP/TFTP.
  --dhcp-interface IFACE      Interface for dnsmasq DHCP/TFTP.
  --dhcp-range RANGE          dnsmasq DHCP range, e.g. 10.10.0.100,10.10.0.200,12h.
  --dhcp-router IP            Optional DHCP router.
  --dhcp-dns IP               Optional DHCP DNS server.
  --uefi-ipxe-source NAME     UEFI iPXE source file: ipxe.efi, snponly.efi, or auto.
  --no-http                   Do not run maas-offline-oneclick.sh.
  --no-collector              Do not install/start Stage1 collector service.
  --dry-run                   Print commands without changing the host.
  -h, --help                  Show this help.

Notes:
  DHCP/TFTP is disabled by default to avoid conflicts with MAAS.
  Use --enable-dhcp only while the target subnet is in diskless_stage1 mode.
EOF
}

log() {
  printf '[diskless-stage1] %s\n' "$*"
}

run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '[dry-run] %q' "$1"
    shift || true
    for arg in "$@"; do
      printf ' %q' "$arg"
    done
    printf '\n'
    return 0
  fi
  "$@"
}

ensure_package_binary() {
  local binary="$1"
  shift
  if command -v "$binary" >/dev/null 2>&1; then
    return 0
  fi
  if [ "$#" -eq 0 ]; then
    echo "missing required binary: $binary" >&2
    exit 1
  fi
  log "installing packages for $binary: $*"
  run sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$@"
  if [ "$DRY_RUN" -eq 0 ] && ! command -v "$binary" >/dev/null 2>&1; then
    echo "failed to install required binary: $binary" >&2
    exit 1
  fi
}

ensure_path_exists() {
  local path="$1"
  shift
  if [ -e "$path" ]; then
    return 0
  fi
  if [ "$#" -eq 0 ]; then
    echo "missing required path: $path" >&2
    exit 1
  fi
  log "installing packages for ${path}: $*"
  run sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$@"
  if [ "$DRY_RUN" -eq 0 ] && [ ! -e "$path" ]; then
    echo "failed to provision required path: $path" >&2
    exit 1
  fi
}

find_first_file() {
  local base="$1"
  shift
  local name
  for name in "$@"; do
    if [ -f "${base}/${name}" ]; then
      printf '%s\n' "${base}/${name}"
      return 0
    fi
  done
  return 1
}

ensure_file_copy() {
  local src="$1"
  local dest="$2"
  if [ "$DRY_RUN" -eq 1 ] || [ ! -f "$dest" ] || ! cmp -s "$src" "$dest"; then
    run sudo install -m 0644 "$src" "$dest"
  fi
}

find_stage1_iso() {
  local candidate
  if [ -n "$STAGE1_ISO_NAME" ]; then
    candidate="${OFFLINE_ROOT}/iso/${STAGE1_ISO_NAME}"
    if [ -f "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
    return 1
  fi

  while IFS= read -r candidate; do
    [ -n "$candidate" ] || continue
    case "$(basename "$candidate")" in
      ._*) continue ;;
    esac
    printf '%s\n' "$candidate"
    return 0
  done < <(find "${OFFLINE_ROOT}/iso" -maxdepth 1 -type f -name 'ubuntu-22.04*live-server-amd64.iso' 2>/dev/null | sort)

  return 1
}

ensure_diskless_payloads() {
  local casper_dir="${OFFLINE_ROOT}/iso/casper"
  local kernel_src initrd_src squashfs_src iso_src

  kernel_src="$(find_first_file "$casper_dir" vmlinuz)" || kernel_src=""
  initrd_src="$(find_first_file "$casper_dir" initrd initrd.img)" || initrd_src=""
  squashfs_src="$(find_first_file "$casper_dir" rootfs.squashfs filesystem.squashfs ubuntu-server-minimal.squashfs)" || squashfs_src=""
  iso_src="$(find_stage1_iso)" || iso_src=""

  if [ -z "$kernel_src" ] || [ -z "$initrd_src" ] || [ -z "$iso_src" ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
      log "would copy diskless payloads from ${casper_dir}"
      return 0
    fi
    [ -n "$kernel_src" ] || { echo "missing diskless kernel in ${casper_dir}" >&2; exit 1; }
    [ -n "$initrd_src" ] || { echo "missing diskless initrd in ${casper_dir}" >&2; exit 1; }
    [ -n "$iso_src" ] || { echo "missing Ubuntu 22.04 live-server ISO under ${OFFLINE_ROOT}/iso" >&2; exit 1; }
  fi

  ensure_file_copy "$kernel_src" "${DISKLESS_DIR}/vmlinuz"
  ensure_file_copy "$initrd_src" "${DISKLESS_DIR}/initrd"
  if [ -n "$squashfs_src" ]; then
    ensure_file_copy "$squashfs_src" "${DISKLESS_DIR}/rootfs.squashfs"
  fi
}

resolve_uefi_ipxe_source() {
  case "$UEFI_IPXE_SOURCE" in
    ipxe.efi)
      find_first_file /usr/lib/ipxe ipxe.efi
      ;;
    snponly.efi)
      find_first_file /usr/lib/ipxe snponly.efi
      ;;
    auto)
      find_first_file /usr/lib/ipxe ipxe.efi snponly.efi
      ;;
    *)
      echo "unsupported --uefi-ipxe-source: ${UEFI_IPXE_SOURCE}" >&2
      exit 2
      ;;
  esac
}

ensure_ipxe_assets() {
  local ipxe_efi_src undionly_src ipxe_efi_name
  ipxe_efi_src="$(resolve_uefi_ipxe_source)" || ipxe_efi_src=""
  undionly_src="$(find_first_file /usr/lib/ipxe undionly.kpxe)" || undionly_src=""
  ipxe_efi_name="$(basename "${ipxe_efi_src:-}")"

  if [ -z "$ipxe_efi_src" ] || [ -z "$undionly_src" ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
      log "would copy iPXE boot assets into ${TFTP_DIR}"
      return 0
    fi
    [ -n "$ipxe_efi_src" ] || { echo "missing requested iPXE EFI binary (${UEFI_IPXE_SOURCE}) under /usr/lib/ipxe" >&2; exit 1; }
    [ -n "$undionly_src" ] || { echo "missing undionly.kpxe under /usr/lib/ipxe" >&2; exit 1; }
  fi

  log "using UEFI iPXE source: ${ipxe_efi_name:-missing}"
  ensure_file_copy "$ipxe_efi_src" "${TFTP_DIR}/ipxe.efi"
  ensure_file_copy "$undionly_src" "${TFTP_DIR}/undionly.kpxe"
  if [ "$DRY_RUN" -eq 1 ]; then
    log "would write ${TFTP_DIR}/ipxe.efi.source"
  else
    printf '%s\n' "${ipxe_efi_name}" | sudo tee "${TFTP_DIR}/ipxe.efi.source" >/dev/null
  fi
}

require_value() {
  local name="$1"
  local value="$2"
  if [ -z "$value" ]; then
    echo "missing required option: $name" >&2
    exit 2
  fi
}

detect_server_ip() {
  if [ -n "$SERVER_IP" ]; then
    return 0
  fi
  SERVER_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
  if [ -z "$SERVER_IP" ]; then
    SERVER_IP="127.0.0.1"
  fi
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --server-ip) SERVER_IP="${2:-}"; shift 2 ;;
    --offline-root) OFFLINE_ROOT="${2:-}"; shift 2 ;;
    --http-port) HTTP_PORT="${2:-}"; shift 2 ;;
    --stage1-port) STAGE1_PORT="${2:-}"; shift 2 ;;
    --stage1-iso-name) STAGE1_ISO_NAME="${2:-}"; shift 2 ;;
    --enable-dhcp) ENABLE_DHCP=1; shift ;;
    --dhcp-interface) DHCP_INTERFACE="${2:-}"; shift 2 ;;
    --dhcp-range) DHCP_RANGE="${2:-}"; shift 2 ;;
    --dhcp-router) DHCP_ROUTER="${2:-}"; shift 2 ;;
    --dhcp-dns) DHCP_DNS="${2:-}"; shift 2 ;;
    --uefi-ipxe-source) UEFI_IPXE_SOURCE="${2:-}"; shift 2 ;;
    --no-http) ENABLE_HTTP=0; shift ;;
    --no-collector) ENABLE_COLLECTOR=0; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage; exit 2 ;;
  esac
done

detect_server_ip

DISKLESS_DIR="${OFFLINE_ROOT}/diskless/ubuntu-22.04"
TFTP_DIR="${OFFLINE_ROOT}/diskless/tftp"
STAGE1_DIR="${OFFLINE_ROOT}/stage1"
NOCLOUD_DIR="${DISKLESS_DIR}/nocloud"
OPT_DIR=/opt/maas-offline
ETC_DIR=/etc/maas-offline
SYSTEMD_DIR=/etc/systemd/system

INSTALL_DHCP_UNIT=0
if [ "$ENABLE_DHCP" -eq 1 ] || { [ -n "$DHCP_INTERFACE" ] && [ -n "$DHCP_RANGE" ]; }; then
  INSTALL_DHCP_UNIT=1
fi

if [ "$ENABLE_DHCP" -eq 1 ]; then
  require_value "--dhcp-interface" "$DHCP_INTERFACE"
  require_value "--dhcp-range" "$DHCP_RANGE"
fi

log "server_ip=${SERVER_IP}"
log "offline_root=${OFFLINE_ROOT}"
log "diskless_dir=${DISKLESS_DIR}"
log "stage1_collector=http://${SERVER_IP}:${STAGE1_PORT}"
STAGE1_ISO_PATH="$(find_stage1_iso || true)"
if [ -n "$STAGE1_ISO_PATH" ]; then
  STAGE1_ISO_NAME="$(basename "$STAGE1_ISO_PATH")"
fi
STAGE1_ISO_URL="http://${SERVER_IP}:${HTTP_PORT}/iso/${STAGE1_ISO_NAME:-ubuntu-22.04-live-server-amd64.iso}"
log "stage1_iso=${STAGE1_ISO_URL}"

run sudo mkdir -p "$DISKLESS_DIR" "$TFTP_DIR" "$STAGE1_DIR" "$NOCLOUD_DIR" "$OPT_DIR" "$ETC_DIR"
run sudo cp "$SCRIPT_DIR/scripts/stage1_collector.py" "$OPT_DIR/stage1_collector.py"
run sudo chmod +x "$OPT_DIR/stage1_collector.py"

if [ "$INSTALL_DHCP_UNIT" -eq 1 ]; then
  ensure_package_binary dnsmasq dnsmasq-base
  ensure_path_exists /usr/lib/ipxe ipxe
fi

if [ ! -f "${STAGE1_DIR}/inventory.csv" ] || [ "$DRY_RUN" -eq 1 ]; then
  run sudo cp "$SCRIPT_DIR/stage1/inventory.example.csv" "${STAGE1_DIR}/inventory.csv"
fi
if [ ! -f "${STAGE1_DIR}/defaults.yaml" ] || [ "$DRY_RUN" -eq 1 ]; then
  run sudo cp "$SCRIPT_DIR/stage1/defaults.example.yaml" "${STAGE1_DIR}/defaults.yaml"
fi
if [ ! -f "${STAGE1_DIR}/state.json" ] || [ "$DRY_RUN" -eq 1 ]; then
  if [ "$DRY_RUN" -eq 1 ]; then
    log "would initialize ${STAGE1_DIR}/state.json"
  else
    echo '{"reports":{}}' | sudo tee "${STAGE1_DIR}/state.json" >/dev/null
  fi
fi

if [ "$DRY_RUN" -eq 1 ]; then
  log "would render ${DISKLESS_DIR}/stage1.ipxe"
  log "would render ${NOCLOUD_DIR}/user-data"
else
  sed \
    -e "s|\${base-url}|http://${SERVER_IP}:${HTTP_PORT}/diskless/ubuntu-22.04|g" \
    -e "s|\${collector-url}|http://${SERVER_IP}:${STAGE1_PORT}|g" \
    -e "s|\${iso-url}|${STAGE1_ISO_URL}|g" \
    -e "s|\${server-ip}|${SERVER_IP}|g" \
    "$SCRIPT_DIR/diskless/stage1.ipxe" | sudo tee "${DISKLESS_DIR}/stage1.ipxe" >/dev/null
  sudo install -m 0644 "$SCRIPT_DIR/diskless/nocloud/meta-data" "${NOCLOUD_DIR}/meta-data"
  sudo install -m 0644 "$SCRIPT_DIR/diskless/nocloud/vendor-data" "${NOCLOUD_DIR}/vendor-data"
  sed \
    -e "s|__STAGE1_SCRIPT_URL__|http://${SERVER_IP}:${HTTP_PORT}/diskless/ubuntu-22.04/nocloud/stage1-report.py|g" \
    -e "s|__TOOLS_REPO_URL__|http://${SERVER_IP}:${HTTP_PORT}/tools/lldpd-mini-repo|g" \
    "$SCRIPT_DIR/diskless/nocloud/user-data" | sudo tee "${NOCLOUD_DIR}/user-data" >/dev/null
  sed \
    -e "s|__COLLECTOR_URL__|http://${SERVER_IP}:${STAGE1_PORT}|g" \
    "$SCRIPT_DIR/diskless/nocloud/stage1-report.py" | sudo tee "${NOCLOUD_DIR}/stage1-report.py" >/dev/null
  sudo chmod 0755 "${NOCLOUD_DIR}/stage1-report.py"
fi

ensure_diskless_payloads

if [ "$ENABLE_HTTP" -eq 1 ]; then
  run "$SCRIPT_DIR/maas-offline-oneclick.sh"
fi

if [ "$ENABLE_COLLECTOR" -eq 1 ]; then
  ensure_package_binary ipmitool ipmitool
  if [ "$DRY_RUN" -eq 1 ]; then
    log "would write ${ETC_DIR}/stage1-collector.env"
  else
    sudo tee "${ETC_DIR}/stage1-collector.env" >/dev/null <<EOF
STAGE1_INVENTORY=${STAGE1_DIR}/inventory.csv
STAGE1_DEFAULTS=${STAGE1_DIR}/defaults.yaml
STAGE1_STATE=${STAGE1_DIR}/state.json
STAGE1_HOST=0.0.0.0
STAGE1_PORT=${STAGE1_PORT}
EOF
  fi
  run sudo cp "$SCRIPT_DIR/systemd/stage1-collector.service" "${SYSTEMD_DIR}/stage1-collector.service"
  run sudo systemctl daemon-reload
  run sudo systemctl enable --now stage1-collector.service
fi

if [ "$INSTALL_DHCP_UNIT" -eq 1 ]; then
  require_value "--dhcp-interface" "$DHCP_INTERFACE"
  require_value "--dhcp-range" "$DHCP_RANGE"
  ensure_ipxe_assets
  if [ "$DRY_RUN" -eq 1 ]; then
    log "would write ${ETC_DIR}/diskless-stage1-dnsmasq.conf"
  else
    sudo tee "${ETC_DIR}/diskless-stage1-dnsmasq.conf" >/dev/null <<EOF
interface=${DHCP_INTERFACE}
bind-interfaces
port=0
log-dhcp
dhcp-range=${DHCP_RANGE}
dhcp-userclass=set:ipxe,iPXE
dhcp-match=set:efi-x86_64,option:client-arch,7
dhcp-match=set:efi-x86_64,option:client-arch,9
dhcp-boot=tag:efi-x86_64,ipxe.efi
dhcp-boot=undionly.kpxe
dhcp-boot=tag:ipxe,http://${SERVER_IP}:${HTTP_PORT}/diskless/ubuntu-22.04/stage1.ipxe
enable-tftp
tftp-root=${TFTP_DIR}
EOF
    if [ -n "$DHCP_ROUTER" ]; then
      echo "dhcp-option=option:router,${DHCP_ROUTER}" | sudo tee -a "${ETC_DIR}/diskless-stage1-dnsmasq.conf" >/dev/null
    fi
    if [ -n "$DHCP_DNS" ]; then
      echo "dhcp-option=option:dns-server,${DHCP_DNS}" | sudo tee -a "${ETC_DIR}/diskless-stage1-dnsmasq.conf" >/dev/null
    fi
  fi
  run sudo cp "$SCRIPT_DIR/systemd/diskless-stage1-dnsmasq.service" "${SYSTEMD_DIR}/diskless-stage1-dnsmasq.service"
  run sudo systemctl daemon-reload
  if [ "$ENABLE_DHCP" -eq 1 ]; then
    run sudo systemctl enable --now diskless-stage1-dnsmasq.service
  else
    run sudo systemctl disable --now diskless-stage1-dnsmasq.service
    log "DHCP/TFTP unit installed but not started. Use maas_pxe_mode.sh diskless_stage1 to start it."
  fi
else
  log "DHCP/TFTP not configured. Provide --dhcp-interface and --dhcp-range to install the switchable unit."
fi

cat <<EOF
diskless_stage1_ready=true
server_ip=${SERVER_IP}
collector_url=http://${SERVER_IP}:${STAGE1_PORT}
diskless_url=http://${SERVER_IP}:${HTTP_PORT}/diskless/ubuntu-22.04/
uefi_ipxe_source=${UEFI_IPXE_SOURCE}
stage1_inventory=${STAGE1_DIR}/inventory.csv
stage1_defaults=${STAGE1_DIR}/defaults.yaml
stage1_state=${STAGE1_DIR}/state.json
EOF
