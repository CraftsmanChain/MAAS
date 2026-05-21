#!/bin/bash

set -euo pipefail

PROFILE="${PROFILE:-admin}"
CSV="${1:?usage: $0 nodes.csv}"

python3 - "$CSV" <<'PY' | while IFS=$'\t' read -r hostname pxe_mac bmc_ip bmc_user bmc_pass tags; do
import csv
import sys

csv_path = sys.argv[1]
with open(csv_path, "r", encoding="utf-8-sig", newline="") as handle:
    reader = csv.DictReader(handle)
    for row in reader:
        hostname = (row.get("hostname") or "").strip()
        pxe_mac = (row.get("pxe_mac") or "").strip()
        bmc_ip = (row.get("bmc_ip") or "").strip()
        bmc_user = (row.get("bmc_user") or "").strip()
        bmc_pass = (row.get("bmc_pass") or "").strip()
        tags = str(row.get("tags") or row.get("tag") or "").strip()
        if not hostname:
            continue
        print("\t".join([hostname, pxe_mac, bmc_ip, bmc_user, bmc_pass, tags]))
PY
  [ -n "${hostname:-}" ] || continue

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
  IFS=',' read -ra tag_arr <<< "${tags//;/,}"
  for t in "${tag_arr[@]:-}"; do
    t="$(echo "$t" | xargs)"
    [ -n "$t" ] || continue
    maas "$PROFILE" tags create name="$t" >/dev/null 2>&1 || true
    maas "$PROFILE" tag update-nodes "$t" add="$sysid" >/dev/null
  done
done
