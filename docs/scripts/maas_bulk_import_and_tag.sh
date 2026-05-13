#!/bin/bash

set -euo pipefail

PROFILE="${PROFILE:-admin}"
CSV="${1:?usage: $0 nodes.csv}"

while IFS=, read -r hostname pxe_mac bmc_ip bmc_user bmc_pass tags; do
  [ -n "${hostname:-}" ] || continue
  [ "${hostname}" != "hostname" ] || continue

  sysid="$(
    maas "$PROFILE" machines read hostname="$hostname" 2>/dev/null \
      | python3 -c 'import sys,json; a=json.load(sys.stdin); print(a[0]["system_id"] if a else "")' || true
  )"

  if [ -z "$sysid" ]; then
    maas "$PROFILE" machines create \
      hostname="$hostname" \
      architecture="amd64/generic" \
      mac_addresses="$pxe_mac" \
      power_type="ipmi" \
      power_parameters_power_address="$bmc_ip" \
      power_parameters_power_user="$bmc_user" \
      power_parameters_power_pass="$bmc_pass" >/dev/null

    sysid="$(
      maas "$PROFILE" machines read hostname="$hostname" \
        | python3 -c 'import sys,json; a=json.load(sys.stdin); print(a[0]["system_id"] if a else "")'
    )"
  fi

  tags="${tags%\"}"
  tags="${tags#\"}"
  IFS=',' read -ra tag_arr <<< "$tags"
  for t in "${tag_arr[@]:-}"; do
    t="$(echo "$t" | xargs)"
    [ -n "$t" ] || continue
    maas "$PROFILE" tags create name="$t" >/dev/null 2>&1 || true
    maas "$PROFILE" tag update-nodes "$t" add="$sysid" >/dev/null
  done
done < "$CSV"

