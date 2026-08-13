#!/bin/bash

set -euo pipefail

PROFILE="${PROFILE:-admin}"
ARCH="${ARCH:-amd64}"
FLOW_TAG="${FLOW_TAG:-}"
CSV="${1:?usage: $0 nodes.csv}"

python3 - "$CSV" <<'PY' | while IFS=$'\x1f' read -r hostname pxe_mac bmc_ip bmc_user bmc_pass node_id tags power_type power_fallback boot_mode; do
import csv
import sys

def clean(value):
    text = str(value or "").strip()
    return "" if text in {"-", "--", "null", "None", "NONE"} else text

def primary_ip(value):
    return clean(str(value).split(",", 1)[0].split("/", 1)[0])

def normalize_boot_mode(value):
    text = clean(value).lower()
    if text in {"bios", "legacy", "legacy boot"}:
        return "legacy"
    if text in {"uefi", "efi", "efi boot"}:
        return "efi"
    return "auto"

csv_path = sys.argv[1]
with open(csv_path, "r", encoding="utf-8-sig", newline="") as handle:
    reader = csv.DictReader(handle)
    for row in reader:
        hostname = clean(row.get("hostname"))
        pxe_mac = clean(row.get("pxe_mac"))
        bmc_ip = primary_ip(row.get("bmc_ip"))
        bmc_user = clean(row.get("bmc_user"))
        bmc_pass = clean(row.get("bmc_pass"))
        node_id = clean(row.get("node_id")) or clean(row.get("sn")) or "System.Embedded.1"
        tags = clean(row.get("tags") or row.get("tag"))
        power_type = clean(row.get("power_driver")) or "ipmi"
        power_fallback = clean(row.get("power_driver_fallback"))
        boot_mode = normalize_boot_mode(row.get("boot_mode"))
        if not hostname:
            continue
        missing = [
            name for name, value in (
                ("pxe_mac", pxe_mac),
                ("bmc_ip", bmc_ip),
                ("bmc_user", bmc_user),
                ("bmc_pass", bmc_pass),
            ) if not value
        ]
        if missing:
            raise SystemExit(f"{hostname}: missing required planned fields: {', '.join(missing)}")
        print("\x1f".join([hostname, pxe_mac, bmc_ip, bmc_user, bmc_pass, node_id, tags, power_type, power_fallback, boot_mode]))
PY
  [ -n "${hostname:-}" ] || continue

  if [ -n "$FLOW_TAG" ]; then
    case ",${tags// /}," in
      *",$FLOW_TAG,"*) ;;
      *) tags="${tags:+$tags,}$FLOW_TAG" ;;
    esac
  fi

  machine_system_id() {
    local output
    if ! output="$(maas "$PROFILE" machines read hostname="$hostname" 2>&1)"; then
      printf 'MAAS query failed for hostname=%s: %s\n' "$hostname" "$output" >&2
      return 1
    fi
    python3 - "$hostname" "$output" <<'PY'
import json
import sys

hostname, raw = sys.argv[1:]
try:
    data = json.loads(raw)
except json.JSONDecodeError as exc:
    raise SystemExit(f"MAAS returned non-JSON for hostname={hostname}: {raw[:500]!r} ({exc})")
if not isinstance(data, list):
    raise SystemExit(f"MAAS returned unexpected payload for hostname={hostname}: {type(data).__name__}")
print(data[0].get("system_id", "") if data else "")
PY
  }

  create_machine() {
    local driver="$1"
    shift
    local args=(
      hostname="$hostname"
      architecture="$ARCH"
      mac_addresses="$pxe_mac"
      power_type="$driver"
      power_parameters_power_address="$bmc_ip"
      power_parameters_power_user="$bmc_user"
      power_parameters_power_pass="$bmc_pass"
    )
    if [ "$driver" = "ipmi" ]; then
      args+=(
        power_parameters_power_boot_type="$boot_mode"
        power_parameters_power_driver="LAN_2_0"
      )
    fi
    if [ "$driver" = "redfish" ] && [ -n "$node_id" ]; then
      args+=(power_parameters_node_id="$node_id")
    fi
    maas "$PROFILE" machines create "${args[@]}" >/dev/null
  }

  update_machine_power() {
    local sysid="$1"
    local driver="$2"
    shift 2
    local args=(
      power_type="$driver"
      power_parameters_power_address="$bmc_ip"
      power_parameters_power_user="$bmc_user"
      power_parameters_power_pass="$bmc_pass"
    )
    if [ "$driver" = "ipmi" ]; then
      args+=(
        power_parameters_power_boot_type="$boot_mode"
        power_parameters_power_driver="LAN_2_0"
      )
    fi
    if [ "$driver" = "redfish" ] && [ -n "$node_id" ]; then
      args+=(power_parameters_node_id="$node_id")
    fi
    maas "$PROFILE" machine update "$sysid" "${args[@]}" >/dev/null
  }

  sysid="$(machine_system_id)"

  if [ -z "$sysid" ]; then
    if ! create_machine "$power_type"; then
      [ -n "$power_fallback" ] && [ "$power_fallback" != "$power_type" ] && create_machine "$power_fallback"
    fi

    sysid="$(machine_system_id)"
  else
    if ! update_machine_power "$sysid" "$power_type"; then
      [ -n "$power_fallback" ] && [ "$power_fallback" != "$power_type" ] && update_machine_power "$sysid" "$power_fallback"
    fi
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
