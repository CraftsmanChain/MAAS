#!/bin/bash

set -euo pipefail

PROFILE=admin
INTERFACE=""
START_IP=""
END_IP=""
GATEWAY_IP=""
DNS_IP=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --profile) PROFILE="${2:-}"; shift 2 ;;
    --interface) INTERFACE="${2:-}"; shift 2 ;;
    --start-ip) START_IP="${2:-}"; shift 2 ;;
    --end-ip) END_IP="${2:-}"; shift 2 ;;
    --gateway) GATEWAY_IP="${2:-}"; shift 2 ;;
    --dns) DNS_IP="${2:-}"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

for value in "$INTERFACE" "$START_IP" "$END_IP"; do
  [ -n "$value" ] || { echo "interface/start-ip/end-ip are required" >&2; exit 2; }
done

rack_id="$(maas "$PROFILE" rack-controllers read | jq -r '.[0].system_id // empty')"
[ -n "$rack_id" ] || { echo "no MAAS rack controller found" >&2; exit 1; }

interface_json="$(maas "$PROFILE" interfaces read "$rack_id" | jq -c --arg name "$INTERFACE" '.[] | select(.name==$name)')"
[ -n "$interface_json" ] || { echo "MAAS interface not found: ${INTERFACE}" >&2; exit 1; }
fabric_id="$(jq -r '.vlan.fabric_id' <<<"$interface_json")"
vid="$(jq -r '.vlan.vid' <<<"$interface_json")"
subnet_id="$(maas "$PROFILE" subnets read | python3 -c '
import ipaddress, json, sys
address = ipaddress.ip_address(sys.argv[1])
for subnet in json.load(sys.stdin):
    if address in ipaddress.ip_network(subnet["cidr"]):
        print(subnet["id"])
        break
' "$START_IP")"
[ -n "$subnet_id" ] || { echo "MAAS subnet not found for ${START_IP}" >&2; exit 1; }

args=()
[ -n "$GATEWAY_IP" ] && args+=("gateway_ip=${GATEWAY_IP}")
[ -n "$DNS_IP" ] && args+=("dns_servers=${DNS_IP}")
[ "${#args[@]}" -eq 0 ] || maas "$PROFILE" subnet update "$subnet_id" "${args[@]}" >/dev/null

if ! maas "$PROFILE" ipranges read | jq -e --arg start_ip "$START_IP" --arg end_ip "$END_IP" --argjson subnet_id "$subnet_id" '.[] | select(.start_ip==$start_ip and .end_ip==$end_ip and .subnet.id==$subnet_id)' >/dev/null; then
  maas "$PROFILE" ipranges create type=dynamic start_ip="$START_IP" end_ip="$END_IP" subnet="$subnet_id" comment='MAAS console PXE pool' >/dev/null
fi

maas "$PROFILE" vlan update "$fabric_id" "$vid" primary_rack="$rack_id" dhcp_on=true >/dev/null
systemctl restart maas-rackd.service
for _ in $(seq 1 60); do
  if [ -s /var/lib/maas/dhcpd-interfaces ] \
    && grep -Fxq "$INTERFACE" /var/lib/maas/dhcpd-interfaces \
    && [ -s /var/lib/maas/dhcpd.conf ]; then
    break
  fi
  sleep 1
done
[ -s /var/lib/maas/dhcpd-interfaces ] || { echo "/var/lib/maas/dhcpd-interfaces was not generated" >&2; exit 1; }
grep -Fxq "$INTERFACE" /var/lib/maas/dhcpd-interfaces || { echo "MAAS DHCP interface was not generated: ${INTERFACE}" >&2; exit 1; }
[ -s /var/lib/maas/dhcpd.conf ] || { echo "/var/lib/maas/dhcpd.conf was not generated" >&2; exit 1; }

# rackd regenerates the DHCP configuration asynchronously and may also restart
# dhcpd itself. Retry only after both generated files are ready, then wait for
# the service to settle instead of failing on that transient state.
for _ in $(seq 1 30); do
  systemctl is-active --quiet maas-dhcpd.service && break
  systemctl restart maas-dhcpd.service || true
  sleep 1
done
systemctl is-active --quiet maas-dhcpd.service || {
  echo "maas-dhcpd.service did not become active" >&2
  journalctl -u maas-rackd.service -u maas-dhcpd.service -n 40 --no-pager >&2 || true
  exit 1
}
echo "maas_dhcp_ready=true interface=${INTERFACE} range=${START_IP}-${END_IP} rack=${rack_id}"
