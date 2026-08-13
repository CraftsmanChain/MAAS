#!/bin/bash

set -euo pipefail

DRY_RUN=0
MODE="${1:-}"
OFFLINE_ROOT=/srv/maas-offline
HTTP_PORT=8083
STAGE1_PORT=8091
MAAS_API_URL="${MAAS_API_URL:-http://127.0.0.1:5240/MAAS/api/2.0/version/}"
CONFIG_DIR=/etc/maas-offline
SYSTEMD_DIR=/etc/systemd/system
IPXE_DIR=/usr/lib/ipxe
SKIP_STAGE1_VALIDATION=0
MAAS_DHCP_INTERFACE="${MAAS_DHCP_INTERFACE:-}"
MAAS_DHCP_START_IP="${MAAS_DHCP_START_IP:-}"
MAAS_DHCP_END_IP="${MAAS_DHCP_END_IP:-}"
MAAS_DHCP_GATEWAY="${MAAS_DHCP_GATEWAY:-}"
MAAS_DHCP_DNS="${MAAS_DHCP_DNS:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUDO=(sudo -n)
if [ "$(id -u)" -eq 0 ]; then
  SUDO=()
fi

usage() {
  cat <<'EOF'
Usage: maas_pxe_mode.sh MODE [--dry-run]

MODE:
  diskless_stage1       Stop MAAS rack PXE path, start Stage1 diskless services.
  maas_provision        Stop Stage1 diskless DHCP/TFTP, start MAAS rack service.
  maintenance_locked    Stop both PXE control planes.
  status                Show relevant service status.

Notes:
  This is the short-term time-exclusive PXE control model.
  It assumes MAAS DHCP/TFTP is served by maas-rackd and Stage1 DHCP/TFTP by
  diskless-stage1-dnsmasq.service.

Options:
  --offline-root PATH       Offline root path for Stage1 assets.
  --http-port PORT          HTTP port for Stage1 iPXE script.
  --stage1-port PORT        Stage1 collector port.
  --config-dir PATH         Stage1 config directory.
  --systemd-dir PATH        systemd unit directory.
  --ipxe-dir PATH           Source iPXE directory.
  --skip-stage1-validation  Skip Stage1 PXE validation before switching.
  --maas-dhcp-interface IF  MAAS provisioning interface to configure.
  --maas-dhcp-start IP      MAAS dynamic range start.
  --maas-dhcp-end IP        MAAS dynamic range end.
  --maas-dhcp-gateway IP    Provisioning subnet gateway.
  --maas-dhcp-dns IP        Provisioning subnet DNS server.
EOF
}

log() {
  printf '[pxe-mode] %s\n' "$*"
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

stop_unit() {
  local unit="$1"
  if [ "$DRY_RUN" -eq 1 ]; then
    run "${SUDO[@]}" systemctl stop "$unit"
    return 0
  fi
  if systemctl list-unit-files "$unit" >/dev/null 2>&1 || systemctl status "$unit" >/dev/null 2>&1; then
    "${SUDO[@]}" systemctl stop "$unit" || true
  else
    log "skip stop missing unit: $unit"
  fi
}

start_unit() {
  local unit="$1"
  run "${SUDO[@]}" systemctl start "$unit"
}

validate_stage1() {
  local args=(
    "${SCRIPT_DIR}/validate_stage1_pxe.sh"
    --offline-root "$OFFLINE_ROOT"
    --http-port "$HTTP_PORT"
    --stage1-port "$STAGE1_PORT"
    --config-dir "$CONFIG_DIR"
    --systemd-dir "$SYSTEMD_DIR"
    --ipxe-dir "$IPXE_DIR"
  )
  if [ "$DRY_RUN" -eq 1 ]; then
    args+=(--skip-systemd-checks)
  fi
  "${args[@]}"
}

unit_status() {
  local unit="$1"
  if command -v systemctl >/dev/null 2>&1; then
    systemctl is-active "$unit" 2>/dev/null || true
  else
    echo "systemctl-not-found"
  fi
}

is_active() {
  [ "$(unit_status "$1")" = "active" ]
}

require_active() {
  local unit="$1"
  if [ "$DRY_RUN" -eq 1 ]; then
    log "would validate active unit: ${unit}"
    return 0
  fi
  if ! is_active "$unit"; then
    echo "mode validation failed: ${unit} is not active" >&2
    return 1
  fi
}

require_inactive() {
  local unit="$1"
  if [ "$DRY_RUN" -eq 1 ]; then
    log "would validate inactive unit: ${unit}"
    return 0
  fi
  if is_active "$unit"; then
    echo "mode validation failed: ${unit} is still active" >&2
    return 1
  fi
}

wait_for_maas_api() {
  if [ "$DRY_RUN" -eq 1 ]; then
    log "would wait for MAAS API: ${MAAS_API_URL}"
    return 0
  fi
  local attempt
  for attempt in $(seq 1 60); do
    if curl -fsS --max-time 3 "$MAAS_API_URL" >/dev/null 2>&1; then
      log "MAAS API ready after ${attempt}s"
      return 0
    fi
    sleep 1
  done
  echo "mode validation failed: MAAS API not ready after 60s: ${MAAS_API_URL}" >&2
  return 1
}

active_mode() {
  local diskless maas_rack maas_dhcp maas_dhcp6
  diskless="$(unit_status diskless-stage1-dnsmasq.service)"
  maas_rack="$(unit_status maas-rackd.service)"
  maas_dhcp="$(unit_status maas-dhcpd.service)"
  maas_dhcp6="$(unit_status maas-dhcpd6.service)"

  if [ "$diskless" = "active" ] && { [ "$maas_rack" = "active" ] || [ "$maas_dhcp" = "active" ] || [ "$maas_dhcp6" = "active" ]; }; then
    echo "conflict"
  elif [ "$diskless" = "active" ]; then
    echo "diskless_stage1"
  elif [ "$maas_rack" = "active" ] || [ "$maas_dhcp" = "active" ] || [ "$maas_dhcp6" = "active" ]; then
    echo "maas_provision"
  else
    echo "maintenance_locked"
  fi
}

validate_mode_after_switch() {
  local expected="$1"
  case "$expected" in
    diskless_stage1)
      require_active maas-offline-http.service
      require_active stage1-collector.service
      require_active diskless-stage1-dnsmasq.service
      require_inactive maas-rackd.service
      require_inactive maas-dhcpd.service
      require_inactive maas-dhcpd6.service
      require_inactive maas-regiond.service
      ;;
    maas_provision)
      require_active maas-offline-http.service
      require_active maas-regiond.service
      require_active maas-rackd.service
      require_inactive diskless-stage1-dnsmasq.service
      require_inactive stage1-collector.service
      ;;
    maintenance_locked)
      require_inactive maas-offline-http.service
      require_inactive stage1-collector.service
      require_inactive diskless-stage1-dnsmasq.service
      require_inactive maas-regiond.service
      require_inactive maas-rackd.service
      require_inactive maas-dhcpd.service
      require_inactive maas-dhcpd6.service
      ;;
  esac

  local actual
  actual="$(active_mode)"
  if [ "$DRY_RUN" -eq 0 ] && [ "$actual" != "$expected" ]; then
    echo "mode validation failed: expected=${expected}, actual=${actual}" >&2
    return 1
  fi
  log "mode validation passed: ${expected}"
}

shift || true
while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --offline-root) OFFLINE_ROOT="${2:-}"; shift 2 ;;
    --http-port) HTTP_PORT="${2:-}"; shift 2 ;;
    --stage1-port) STAGE1_PORT="${2:-}"; shift 2 ;;
    --config-dir) CONFIG_DIR="${2:-}"; shift 2 ;;
    --systemd-dir) SYSTEMD_DIR="${2:-}"; shift 2 ;;
    --ipxe-dir) IPXE_DIR="${2:-}"; shift 2 ;;
    --skip-stage1-validation) SKIP_STAGE1_VALIDATION=1; shift ;;
    --maas-dhcp-interface) MAAS_DHCP_INTERFACE="${2:-}"; shift 2 ;;
    --maas-dhcp-start) MAAS_DHCP_START_IP="${2:-}"; shift 2 ;;
    --maas-dhcp-end) MAAS_DHCP_END_IP="${2:-}"; shift 2 ;;
    --maas-dhcp-gateway) MAAS_DHCP_GATEWAY="${2:-}"; shift 2 ;;
    --maas-dhcp-dns) MAAS_DHCP_DNS="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage; exit 2 ;;
  esac
done

case "$MODE" in
  diskless_stage1)
    log "switching to diskless_stage1"
    if [ "$SKIP_STAGE1_VALIDATION" -ne 1 ]; then
      log "validating Stage1 PXE assets"
      validate_stage1
    fi
    stop_unit maas-rackd.service
    stop_unit maas-dhcpd.service
    stop_unit maas-dhcpd6.service
    stop_unit maas-regiond.service
    start_unit maas-offline-http.service
    start_unit stage1-collector.service
    start_unit diskless-stage1-dnsmasq.service
    validate_mode_after_switch diskless_stage1
    ;;
  maas_provision)
    log "switching to maas_provision"
    stop_unit diskless-stage1-dnsmasq.service
    stop_unit stage1-collector.service
    start_unit maas-offline-http.service
    start_unit maas-regiond.service
    start_unit maas-rackd.service
    wait_for_maas_api
    if [ -n "$MAAS_DHCP_INTERFACE" ] && [ -n "$MAAS_DHCP_START_IP" ] && [ -n "$MAAS_DHCP_END_IP" ]; then
      dhcp_args=(
        --interface "$MAAS_DHCP_INTERFACE"
        --start-ip "$MAAS_DHCP_START_IP"
        --end-ip "$MAAS_DHCP_END_IP"
      )
      [ -z "$MAAS_DHCP_GATEWAY" ] || dhcp_args+=(--gateway "$MAAS_DHCP_GATEWAY")
      [ -z "$MAAS_DHCP_DNS" ] || dhcp_args+=(--dns "$MAAS_DHCP_DNS")
      if [ "$DRY_RUN" -eq 1 ]; then
        log "would ensure MAAS DHCP: ${dhcp_args[*]}"
      else
        "${SCRIPT_DIR}/maas_ensure_dhcp.sh" "${dhcp_args[@]}"
      fi
    fi
    validate_mode_after_switch maas_provision
    ;;
  maintenance_locked)
    log "switching to maintenance_locked"
    stop_unit diskless-stage1-dnsmasq.service
    stop_unit stage1-collector.service
    stop_unit maas-offline-http.service
    stop_unit maas-regiond.service
    stop_unit maas-rackd.service
    stop_unit maas-dhcpd.service
    stop_unit maas-dhcpd6.service
    validate_mode_after_switch maintenance_locked
    ;;
  status)
    printf 'active_mode=%s\n' "$(active_mode)"
    printf 'maas-offline-http=%s\n' "$(unit_status maas-offline-http.service)"
    printf 'stage1-collector=%s\n' "$(unit_status stage1-collector.service)"
    printf 'diskless-stage1-dnsmasq=%s\n' "$(unit_status diskless-stage1-dnsmasq.service)"
    printf 'maas-regiond=%s\n' "$(unit_status maas-regiond.service)"
    printf 'maas-rackd=%s\n' "$(unit_status maas-rackd.service)"
    printf 'maas-dhcpd=%s\n' "$(unit_status maas-dhcpd.service)"
    printf 'maas-dhcpd6=%s\n' "$(unit_status maas-dhcpd6.service)"
    ;;
  -h|--help|"")
    usage
    exit 0
    ;;
  *)
    echo "unknown mode: $MODE" >&2
    usage
    exit 2
    ;;
esac
