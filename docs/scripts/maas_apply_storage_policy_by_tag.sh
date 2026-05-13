#!/bin/bash

set -euo pipefail

PROFILE="${PROFILE:-admin}"
TAG="${1:?usage: $0 tag root_size}"
ROOT_SIZE="${2:?usage: $0 tag root_size}"

set_layout() {
  local sysid="$1"
  local boot_size="$2"
  local root_size="$3"

  if maas "$PROFILE" machine set-storage-layout "$sysid" layout=flat boot_size="$boot_size" root_size="$root_size" >/dev/null 2>&1; then
    return 0
  fi
  maas "$PROFILE" machine set-storage-layout "$sysid" storage_layout=flat boot_size="$boot_size" root_size="$root_size" >/dev/null
}

pick_boot_device_id() {
  local sysid="$1"
  maas "$PROFILE" block-devices read "$sysid" \
    | python3 - "$sysid" <<'PY'
import json, sys
a = json.load(sys.stdin)
phys = [d for d in a if d.get("type") == "physical"]
by_name = {d.get("name"): d for d in phys}
if "sda" in by_name:
    print(by_name["sda"]["id"])
    raise SystemExit(0)
if phys:
    phys.sort(key=lambda x: x.get("size", 0))
    print(phys[0]["id"])
PY
}

maas "$PROFILE" tag machines "$TAG" \
  | python3 -c 'import sys,json; a=json.load(sys.stdin); print("\n".join([m["system_id"] for m in a]))' \
  | while read -r sysid; do
      [ -n "${sysid:-}" ] || continue
      dev_id="$(pick_boot_device_id "$sysid")"
      maas "$PROFILE" block-device set-boot-disk "$sysid" "$dev_id" >/dev/null || true
      set_layout "$sysid" "2G" "$ROOT_SIZE"

      part_id="$(
        maas "$PROFILE" partitions create "$sysid" "$dev_id" \
          | python3 -c 'import sys,json; o=json.load(sys.stdin); print(o["id"])'
      )"

      maas "$PROFILE" partition format "$sysid" "$dev_id" "$part_id" fstype=ext4 >/dev/null
      maas "$PROFILE" partition mount "$sysid" "$dev_id" "$part_id" mount_point=/data >/dev/null
    done

