#!/bin/bash

set -euo pipefail

PROFILE="${PROFILE:-admin}"
TAG="${1:?usage: $0 tag root_size}"
ROOT_SIZE="${2:?usage: $0 tag root_size}"

size_to_bytes() {
  python3 - "$1" <<'PY'
import sys
v = sys.argv[1].strip().upper()
mult = 1
if v.endswith("K"):
    mult = 1024
    v = v[:-1]
elif v.endswith("M"):
    mult = 1024 ** 2
    v = v[:-1]
elif v.endswith("G"):
    mult = 1024 ** 3
    v = v[:-1]
elif v.endswith("T"):
    mult = 1024 ** 4
    v = v[:-1]
print(int(float(v) * mult))
PY
}

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
  maas "$PROFILE" block-devices read "$sysid" | python3 -c '
import json, sys

devices = json.load(sys.stdin)
phys = [d for d in devices if d.get("type") == "physical"]
if not phys:
    raise SystemExit(1)

def rank(dev):
    name = (dev.get("name") or "").lower()
    model = (dev.get("model") or "").lower()
    size = dev.get("size") or 0
    is_nvme = name.startswith("nvme")
    is_sda = name == "sda"
    looks_ssd = ("ssd" in model) or ("mr9560" in model) or (name.startswith("sd") and not is_nvme)
    return (
        0 if is_sda else 1,
        0 if looks_ssd and not is_nvme else 1,
        0 if not is_nvme else 1,
        size,
        name,
    )

phys.sort(key=rank)
print(phys[0]["id"])
'
}

maas "$PROFILE" tag machines "$TAG" \
  | python3 -c 'import sys,json; a=json.load(sys.stdin); print("\n".join([m["system_id"] for m in a]))' \
  | while read -r sysid; do
      [ -n "${sysid:-}" ] || continue
      dev_id="$(pick_boot_device_id "$sysid")"
      [ -n "${dev_id:-}" ] || { echo "no boot device found for ${sysid}" >&2; exit 1; }
      maas "$PROFILE" block-device set-boot-disk "$sysid" "$dev_id" >/dev/null
      set_layout "$sysid" "$(size_to_bytes 2G)" "$(size_to_bytes "$ROOT_SIZE")"

      part_id="$(
        maas "$PROFILE" partitions create "$sysid" "$dev_id" \
          | python3 -c 'import sys,json; o=json.load(sys.stdin); print(o["id"])'
      )"

      maas "$PROFILE" partition format "$sysid" "$dev_id" "$part_id" fstype=ext4 >/dev/null
      maas "$PROFILE" partition mount "$sysid" "$dev_id" "$part_id" mount_point=/data >/dev/null
    done
