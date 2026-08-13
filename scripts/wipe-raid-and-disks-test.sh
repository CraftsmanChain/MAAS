#!/bin/bash
# --- Start MAAS 1.0 script metadata ---
# name: wipe-raid-and-disks-test
# title: wipe-raid-and-disks-test
# description: wipe-raid-and-disks-test
# script_type: testing
# timeout: 00:30:00
# parallel: disabled
# destructive: true
# --- End MAAS 1.0 script metadata ---

set -euxo pipefail
exec > >(tee /tmp/wipe-raid-and-disks.log) 2>&1

# 这些变量由控制台按当前实验室配置渲染后再注册到 MAAS。
BASE_URL="__OFFLINE_TOOL_BASE_URL__"
STAGE1_CONFIG_URL="__STAGE1_CONFIG_URL__"
BOOT_VD_NAME="__BOOT_VD_NAME__"
SINGLE_DISK_RAID_LEVEL="__SINGLE_DISK_RAID_LEVEL__"
MULTI_DISK_RAID_LEVEL="__MULTI_DISK_RAID_LEVEL__"
BOOT_DISK_COUNT="__BOOT_DISK_COUNT__"
DATA_DISK_RAID_LAYOUT='__DATA_DISK_RAID_LAYOUT__'
WORKDIR="/tmp/raid-tools"
mkdir -p "$WORKDIR"

STORCLI=""
MEGACLI=""
SAS3IRCU=""
SAS2IRCU=""

detect_serial_number() {
  local sn=""
  if command -v dmidecode >/dev/null 2>&1; then
    sn="$(dmidecode -s system-serial-number 2>/dev/null | awk 'NF {print; exit}')"
  fi
  if [ -z "$sn" ] && [ -r /sys/class/dmi/id/product_serial ]; then
    sn="$(tr -d '[:space:]' < /sys/class/dmi/id/product_serial)"
  fi
  printf '%s\n' "$sn"
}

urlencode() {
  python3 - "$1" <<'PY'
import sys
from urllib.parse import quote

print(quote(sys.argv[1], safe=""))
PY
}

fetch_node_config_json() {
  local sn=""
  sn="$(detect_serial_number)"
  if [ -z "$sn" ] || [ -z "$STAGE1_CONFIG_URL" ]; then
    return 0
  fi
  case "$STAGE1_CONFIG_URL" in
    __*__) return 0 ;;
  esac
  curl -fsSL "${STAGE1_CONFIG_URL}/$(urlencode "$sn")"
}

apply_node_config_overrides() {
  local config_json=""
  local key=""
  local value=""
  config_json="$(fetch_node_config_json)"
  [ -n "$config_json" ] || return 0

  while IFS=$'\t' read -r key value; do
    case "$key" in
      BOOT_VD_NAME) [ -n "$value" ] && BOOT_VD_NAME="$value" ;;
      SINGLE_DISK_RAID_LEVEL) [ -n "$value" ] && SINGLE_DISK_RAID_LEVEL="$value" ;;
      MULTI_DISK_RAID_LEVEL) [ -n "$value" ] && MULTI_DISK_RAID_LEVEL="$value" ;;
      BOOT_DISK_COUNT) [ -n "$value" ] && BOOT_DISK_COUNT="$value" ;;
      DATA_DISK_RAID_LAYOUT) [ -n "$value" ] && DATA_DISK_RAID_LAYOUT="$value" ;;
    esac
  done < <(
    CONFIG_JSON="$config_json" python3 <<'PY'
import json
import os

try:
    data = json.loads(os.environ.get("CONFIG_JSON") or "{}")
except Exception:
    data = {}

fields = {
    "BOOT_VD_NAME": data.get("boot_vd_name") or "",
    "SINGLE_DISK_RAID_LEVEL": data.get("single_disk_raid_level") or "",
    "MULTI_DISK_RAID_LEVEL": data.get("multi_disk_raid_level") or "",
    "BOOT_DISK_COUNT": str(data.get("boot_disk_count") or ""),
    "DATA_DISK_RAID_LAYOUT": data.get("data_disk_raid_layout") or "",
}
for key, value in fields.items():
    if isinstance(value, (list, dict)):
        value = json.dumps(value, ensure_ascii=False)
    print(f"{key}\t{value}")
PY
  )
}

boot_disk_target_count() {
  local value="${BOOT_DISK_COUNT:-2}"
  if ! [ "$value" -ge 1 ] 2>/dev/null; then
    value=2
  fi
  printf '%s\n' "$value"
}

join_first_n_csv() {
  local limit="$1"
  shift || true
  local out=""
  local count=0
  local item=""
  for item in "$@"; do
    [ "$count" -ge "$limit" ] && break
    out="${out:+$out,}$item"
    count=$((count + 1))
  done
  printf '%s\n' "$out"
}

sasircu_raid_level() {
  local value
  value="$(printf '%s' "${1:-}" | tr '[:lower:]' '[:upper:]')"
  case "$value" in
    R*) printf 'RAID%s\n' "${value#R}" ;;
    RAID*) printf '%s\n' "$value" ;;
    *) printf '%s\n' "$value" ;;
  esac
}

find_bin() {
  for p in "$@"; do
    [ -x "$p" ] && { echo "$p"; return 0; }
  done
  return 1
}

download_file() {
  local name="$1"
  local out="$2"
  curl -fsSL "${BASE_URL}/${name}" -o "$out"
}

ensure_storcli() {
  STORCLI="$(find_bin /opt/MegaRAID/storcli/storcli64 /opt/MegaRAID/StorCLI/storcli64 /usr/local/bin/storcli64 /usr/bin/storcli64 || true)"
  if [ -z "$STORCLI" ]; then
    download_file "storcli_007.2508.0000.0000_all.deb" "$WORKDIR/storcli_007.2508.0000.0000_all.deb"
    dpkg -i "$WORKDIR/storcli_007.2508.0000.0000_all.deb" || true
    STORCLI="$(find_bin /opt/MegaRAID/storcli/storcli64 /opt/MegaRAID/StorCLI/storcli64 /usr/local/bin/storcli64 /usr/bin/storcli64 || true)"
  fi
}

ensure_megacli() {
  MEGACLI="$(find_bin /opt/MegaRAID/MegaCli/MegaCli64 /usr/local/bin/MegaCli64 /usr/bin/MegaCli64 || true)"
  if [ -z "$MEGACLI" ]; then
    download_file "MegaCli64" "$WORKDIR/MegaCli64"
    chmod +x "$WORKDIR/MegaCli64"
    MEGACLI="$WORKDIR/MegaCli64"
  fi
}

ensure_sas3ircu() {
  SAS3IRCU="$(find_bin /tools/sas3ircu /usr/local/bin/sas3ircu /usr/bin/sas3ircu || true)"
  if [ -z "$SAS3IRCU" ]; then
    download_file "sas3ircu" "$WORKDIR/sas3ircu"
    chmod +x "$WORKDIR/sas3ircu"
    SAS3IRCU="$WORKDIR/sas3ircu"
  fi
}

ensure_sas2ircu() {
  SAS2IRCU="$(find_bin /tools/sas2ircu /usr/local/bin/sas2ircu /usr/bin/sas2ircu || true)"
  if [ -z "$SAS2IRCU" ]; then
    download_file "sas2ircu" "$WORKDIR/sas2ircu"
    chmod +x "$WORKDIR/sas2ircu"
    SAS2IRCU="$WORKDIR/sas2ircu"
  fi
}

print_tools() {
  echo "==== TOOLS ===="
  echo "BASE_URL=${BASE_URL}"
  echo "STAGE1_CONFIG_URL=${STAGE1_CONFIG_URL}"
  echo "BOOT_VD_NAME=${BOOT_VD_NAME}"
  echo "SINGLE_DISK_RAID_LEVEL=${SINGLE_DISK_RAID_LEVEL}"
  echo "MULTI_DISK_RAID_LEVEL=${MULTI_DISK_RAID_LEVEL}"
  echo "BOOT_DISK_COUNT=$(boot_disk_target_count)"
  echo "DATA_DISK_RAID_LAYOUT=${DATA_DISK_RAID_LAYOUT}"
  echo "STORCLI=${STORCLI:-}"
  echo "MEGACLI=${MEGACLI:-}"
  echo "SAS3IRCU=${SAS3IRCU:-}"
  echo "SAS2IRCU=${SAS2IRCU:-}"
}

csv_count() {
  local text="${1:-}"
  if [ -z "$text" ]; then
    printf '0\n'
    return 0
  fi
  awk -v value="$text" 'BEGIN { print split(value, parts, ",") }'
}

data_layout_rows() {
  DATA_LAYOUT_JSON="${DATA_DISK_RAID_LAYOUT:-[]}" python3 <<'PY'
import json
import os

text = (os.environ.get("DATA_LAYOUT_JSON") or "").strip()
if not text:
    raise SystemExit(0)
try:
    data = json.loads(text)
except Exception:
    raise SystemExit(0)
if not isinstance(data, list):
    raise SystemExit(0)
for index, item in enumerate(data, 1):
    if not isinstance(item, dict):
        continue
    disk_count = item.get("disk_count")
    try:
        disk_count = int(disk_count)
    except Exception:
        disk_count = 0
    if disk_count <= 0:
        continue
    name = str(item.get("name") or f"data{index:02d}").strip()
    raid_level = str(item.get("raid_level") or "r1").strip()
    print(f"{name}\t{raid_level}\t{disk_count}")
PY
}

wipe_visible_disks() {
  echo "==== WIPE VISIBLE DISKS ===="
  lsblk -dpno NAME,TYPE | awk '$2=="disk"{print $1}' | while read -r d; do
    [ -b "$d" ] || continue
    echo "==== WIPE $d ===="
    wipefs -af "$d" || true
    sgdisk --zap-all "$d" || true
    blkdiscard "$d" || true
  done
  udevadm settle || true
  partprobe || true
  sleep 5
}

storcli_ctrls() {
  "$STORCLI" /call show 2>/dev/null | awk -F'= ' '/^Controller = /{gsub(/^[ \t]+|[ \t]+$/, "", $2); print $2}'
}

storcli_ssds() {
  local ctrl="$1"
  "$STORCLI" "/c$ctrl" /eall /sall show 2>/dev/null \
    | awk '$1 ~ /^[0-9]+:[0-9]+$/ && $7=="SATA" && $8=="SSD" {print $1}' \
    | sort -t: -k1,1n -k2,2n
}

storcli_data_drives() {
  local ctrl="$1"
  "$STORCLI" "/c$ctrl" /eall /sall show 2>/dev/null \
    | awk '$1 ~ /^[0-9]+:[0-9]+$/ && ($7=="SATA" || $7=="SAS") && $8!="SSD" {print $1}' \
    | sort -t: -k1,1n -k2,2n
}

storcli_boot_vd_by_name() {
  local ctrl="$1"
  local target_name="$2"
  "$STORCLI" "/c$ctrl" /vall show 2>/dev/null | awk -v target="$target_name" '
    $0 ~ /^[[:space:]]*[0-9]+\/[0-9]+/ && $NF == target {
      split($1, a, "/")
      print a[2]
      exit
    }'
}

configure_with_storcli() {
  local ctrl="$1"
  mapfile -t ssds < <(storcli_ssds "$ctrl")

  echo "==== STORCLI CTRL $ctrl BEFORE ===="
  "$STORCLI" "/c$ctrl" show || true
  "$STORCLI" "/c$ctrl" /vall show || true
  "$STORCLI" "/c$ctrl" /eall /sall show || true

  "$STORCLI" "/c$ctrl" /vall del force || true
  "$STORCLI" "/c$ctrl" /fall del || true

  udevadm settle || true
  sleep 3

  mapfile -t ssds < <(storcli_ssds "$ctrl")

  echo "==== STORCLI CTRL $ctrl SSDS ===="
  printf '%s\n' "${ssds[@]:-}"

  local created=0
  local boot_vd=""
  local raid_level=""
  local drives_csv=""
  local target_count
  target_count="$(boot_disk_target_count)"

  if [ "$target_count" -le 1 ]; then
    if [ "${#ssds[@]}" -ge 1 ]; then
      raid_level="${SINGLE_DISK_RAID_LEVEL}"
      drives_csv="${ssds[0]}"
    fi
  elif [ "${#ssds[@]}" -ge "$target_count" ]; then
    raid_level="${MULTI_DISK_RAID_LEVEL}"
    drives_csv="$(join_first_n_csv "$target_count" "${ssds[@]}")"
    if [ "${#ssds[@]}" -gt "$target_count" ]; then
      echo "WARN: more than ${target_count} SSDs detected on controller $ctrl, using first ${target_count}: $drives_csv"
    fi
  elif [ "${#ssds[@]}" -eq 1 ]; then
    raid_level="${SINGLE_DISK_RAID_LEVEL}"
    drives_csv="${ssds[0]}"
  else
    echo "WARN: SSD count ${#ssds[@]} is less than configured boot_disk_count=${target_count}, skip VD creation"
  fi

  if [ -n "$raid_level" ] && [ -n "$drives_csv" ]; then
    if "$STORCLI" "/c$ctrl" add vd "$raid_level" name="$BOOT_VD_NAME" drives="$drives_csv"; then
      created=1
    fi
  else
    echo "WARN: no eligible SSDs detected on controller $ctrl, skip VD creation"
  fi

  boot_vd="$(storcli_boot_vd_by_name "$ctrl" "$BOOT_VD_NAME" || true)"
  if [ -n "$boot_vd" ]; then
    "$STORCLI" "/c$ctrl/v$boot_vd" set bootdrive=on || true
  fi

  if [ "${DATA_DISK_RAID_LAYOUT:-[]}" != "[]" ]; then
    mapfile -t data_drives < <(storcli_data_drives "$ctrl")
    while IFS=$'\t' read -r layout_name layout_level layout_count; do
      [ -n "$layout_name" ] || continue
      drives_csv="$(join_first_n_csv "$layout_count" "${data_drives[@]}")"
      if [ "$(csv_count "$drives_csv")" -lt "$layout_count" ]; then
        echo "WARN: controller $ctrl data layout ${layout_name} requires ${layout_count} drives, only $(csv_count "$drives_csv") available"
        continue
      fi
      echo "==== STORCLI CTRL $ctrl DATA ${layout_name} ${layout_level} ${drives_csv} ===="
      if "$STORCLI" "/c$ctrl" add vd "$layout_level" name="$layout_name" drives="$drives_csv"; then
        data_drives=("${data_drives[@]:$layout_count}")
      fi
    done < <(data_layout_rows)
  fi

  echo "==== STORCLI CTRL $ctrl CREATED $created ===="
  "$STORCLI" "/c$ctrl" /vall show || true
  "$STORCLI" "/c$ctrl" show bootdrive || true
}

megacli_ctrls() {
  local count
  count="$("$MEGACLI" -adpCount 2>/dev/null | awk -F': ' '/Controller Count|Adapter Count/{gsub(/\..*$/, "", $2); print $2; exit}')"
  [ -n "${count:-}" ] || count=0
  if [ "$count" -gt 0 ] 2>/dev/null; then
    seq 0 $((count - 1))
  fi
}

megacli_ssds() {
  local ctrl="$1"
  "$MEGACLI" -PDList -a"$ctrl" 2>/dev/null | awk '
    BEGIN{e="";s="";ptype="";mtype=""}
    /Enclosure Device ID:/ {e=$NF}
    /Slot Number:/ {s=$NF}
    /PD Type:/ {ptype=$NF}
    /Media Type:/ {
      mtype=$0
      if (e != "" && s != "" && ptype == "SATA" && mtype ~ /Solid State Device|SSD/) {
        print e ":" s
      }
      e=""; s=""; ptype=""; mtype=""
    }' | sort -t: -k1,1n -k2,2n
}

megacli_data_drives() {
  local ctrl="$1"
  "$MEGACLI" -PDList -a"$ctrl" 2>/dev/null | awk '
    BEGIN{e="";s="";ptype="";mtype=""}
    /Enclosure Device ID:/ {e=$NF}
    /Slot Number:/ {s=$NF}
    /PD Type:/ {ptype=$NF}
    /Media Type:/ {
      mtype=$0
      if (e != "" && s != "" && (ptype == "SATA" || ptype == "SAS") && mtype !~ /Solid State Device|SSD/) {
        print e ":" s
      }
      e=""; s=""; ptype=""; mtype=""
    }' | sort -t: -k1,1n -k2,2n
}

megacli_boot_ld_by_name() {
  local ctrl="$1"
  local target_name="$2"
  "$MEGACLI" -LDInfo -Lall -a"$ctrl" 2>/dev/null | awk -v target="$target_name" '
    /Virtual Drive:/ {vd=$3}
    /^Name[[:space:]]*:/ {
      sub(/^Name[[:space:]]*:[[:space:]]*/, "", $0)
      if ($0 == target) {
        print vd
        exit
      }
    }'
}

configure_with_megacli() {
  local ctrl="$1"
  mapfile -t ssds < <(megacli_ssds "$ctrl")

  echo "==== MEGACLI CTRL $ctrl BEFORE ===="
  "$MEGACLI" -AdpAllInfo -a"$ctrl" || true
  "$MEGACLI" -LDInfo -Lall -a"$ctrl" || true
  "$MEGACLI" -PDList -a"$ctrl" || true

  "$MEGACLI" -CfgLdDel -Lall -a"$ctrl" || true
  "$MEGACLI" -CfgForeign -Clear -a"$ctrl" || true

  udevadm settle || true
  sleep 3

  mapfile -t ssds < <(megacli_ssds "$ctrl")

  echo "==== MEGACLI CTRL $ctrl SSDS ===="
  printf '%s\n' "${ssds[@]:-}"

  local created=0
  local boot_ld=""
  local raid_level=""
  local drives_csv=""
  local target_count
  target_count="$(boot_disk_target_count)"

  if [ "$target_count" -le 1 ]; then
    if [ "${#ssds[@]}" -ge 1 ]; then
      raid_level="${SINGLE_DISK_RAID_LEVEL}"
      drives_csv="${ssds[0]}"
    fi
  elif [ "${#ssds[@]}" -ge "$target_count" ]; then
    raid_level="${MULTI_DISK_RAID_LEVEL}"
    drives_csv="$(join_first_n_csv "$target_count" "${ssds[@]}")"
    if [ "${#ssds[@]}" -gt "$target_count" ]; then
      echo "WARN: more than ${target_count} SSDs detected on controller $ctrl, using first ${target_count}: $drives_csv"
    fi
  elif [ "${#ssds[@]}" -eq 1 ]; then
    raid_level="${SINGLE_DISK_RAID_LEVEL}"
    drives_csv="${ssds[0]}"
  else
    echo "WARN: SSD count ${#ssds[@]} is less than configured boot_disk_count=${target_count}, skip LD creation"
  fi

  if [ -n "$raid_level" ] && [ -n "$drives_csv" ]; then
    if "$MEGACLI" -CfgLdAdd -"${raid_level}"["$drives_csv"] WT NORA Direct -strpsz64 -a"$ctrl"; then
      created=1
    fi
  else
    echo "WARN: no eligible SSDs detected on controller $ctrl, skip LD creation"
  fi

  boot_ld="$(megacli_boot_ld_by_name "$ctrl" "$BOOT_VD_NAME" || true)"
  if [ -n "$boot_ld" ]; then
    "$MEGACLI" -AdpBootDrive -Set -L"$boot_ld" -a"$ctrl" || true
  fi

  if [ "${DATA_DISK_RAID_LAYOUT:-[]}" != "[]" ]; then
    mapfile -t data_drives < <(megacli_data_drives "$ctrl")
    while IFS=$'\t' read -r layout_name layout_level layout_count; do
      [ -n "$layout_name" ] || continue
      drives_csv="$(join_first_n_csv "$layout_count" "${data_drives[@]}")"
      if [ "$(csv_count "$drives_csv")" -lt "$layout_count" ]; then
        echo "WARN: controller $ctrl data layout ${layout_name} requires ${layout_count} drives, only $(csv_count "$drives_csv") available"
        continue
      fi
      echo "==== MEGACLI CTRL $ctrl DATA ${layout_name} ${layout_level} ${drives_csv} ===="
      if "$MEGACLI" -CfgLdAdd -"${layout_level}"["$drives_csv"] WT NORA Direct -strpsz64 -a"$ctrl"; then
        data_drives=("${data_drives[@]:$layout_count}")
      fi
    done < <(data_layout_rows)
  fi

  echo "==== MEGACLI CTRL $ctrl CREATED $created ===="
  "$MEGACLI" -LDInfo -Lall -a"$ctrl" || true
}

sasircu_ctrls() {
  local bin="$1"
  "$bin" LIST 2>/dev/null | awk '$1 ~ /^[0-9]+$/ {print $1}'
}

sasircu_ssds() {
  local bin="$1"
  local ctrl="$2"
  "$bin" "$ctrl" DISPLAY 2>/dev/null | awk '
    BEGIN{enc="";slot="";proto="";dtype=""}
    /Enclosure/ {enc=$NF}
    /Slot/ {slot=$NF}
    /Protocol/ {proto=$NF}
    /Drive Type/ {dtype=$0}
    /^$/ {
      if (enc != "" && slot != "" && proto == "SATA" && dtype ~ /SSD|Solid State/) {
        print enc ":" slot
      }
      enc=""; slot=""; proto=""; dtype=""
    }
    END {
      if (enc != "" && slot != "" && proto == "SATA" && dtype ~ /SSD|Solid State/) {
        print enc ":" slot
      }
    }' | sort -t: -k1,1n -k2,2n
}

sasircu_data_drives() {
  local bin="$1"
  local ctrl="$2"
  "$bin" "$ctrl" DISPLAY 2>/dev/null | awk '
    BEGIN{enc="";slot="";proto="";dtype=""}
    /Enclosure/ {enc=$NF}
    /Slot/ {slot=$NF}
    /Protocol/ {proto=$NF}
    /Drive Type/ {dtype=$0}
    /^$/ {
      if (enc != "" && slot != "" && (proto == "SATA" || proto == "SAS") && dtype !~ /SSD|Solid State/) {
        print enc ":" slot
      }
      enc=""; slot=""; proto=""; dtype=""
    }
    END {
      if (enc != "" && slot != "" && (proto == "SATA" || proto == "SAS") && dtype !~ /SSD|Solid State/) {
        print enc ":" slot
      }
    }' | sort -t: -k1,1n -k2,2n
}

configure_with_sasircu() {
  local bin="$1"
  local ctrl="$2"
  mapfile -t ssds < <(sasircu_ssds "$bin" "$ctrl")

  echo "==== SASIRCU CTRL $ctrl BEFORE ===="
  "$bin" "$ctrl" DISPLAY || true
  "$bin" "$ctrl" DELETE NOPROMPT || true

  udevadm settle || true
  sleep 3

  mapfile -t ssds < <(sasircu_ssds "$bin" "$ctrl")

  echo "==== SASIRCU CTRL $ctrl SSDS ===="
  printf '%s\n' "${ssds[@]:-}"

  local created=0
  local raid_level=""
  local target_count
  local sas_level=""
  target_count="$(boot_disk_target_count)"

  if [ "$target_count" -le 1 ]; then
    if [ "${#ssds[@]}" -ge 1 ]; then
      raid_level="${SINGLE_DISK_RAID_LEVEL}"
      sas_level="$(sasircu_raid_level "$raid_level")"
      if "$bin" "$ctrl" CREATE "$sas_level" MAX "${ssds[0]}" "$BOOT_VD_NAME" NOPROMPT; then
        created=1
      fi
    fi
  elif [ "${#ssds[@]}" -ge "$target_count" ]; then
    raid_level="${MULTI_DISK_RAID_LEVEL}"
    sas_level="$(sasircu_raid_level "$raid_level")"
    if [ "${#ssds[@]}" -gt "$target_count" ]; then
      echo "WARN: more than ${target_count} SSDs detected on controller $ctrl, using first ${target_count}"
    fi
    if [ "$target_count" -eq 2 ] && "$bin" "$ctrl" CREATE "$sas_level" MAX "${ssds[0]}" "${ssds[1]}" "$BOOT_VD_NAME" NOPROMPT; then
      created=1
    elif [ "$target_count" -ne 2 ]; then
      echo "WARN: sasircu path currently supports boot_disk_count=1 or 2, configured=${target_count}"
    fi
  elif [ "${#ssds[@]}" -eq 1 ]; then
    raid_level="${SINGLE_DISK_RAID_LEVEL}"
    sas_level="$(sasircu_raid_level "$raid_level")"
    if "$bin" "$ctrl" CREATE "$sas_level" MAX "${ssds[0]}" "$BOOT_VD_NAME" NOPROMPT; then
      created=1
    fi
  else
    echo "WARN: no eligible SSDs detected on controller $ctrl, skip RAID creation"
  fi

  if [ "${DATA_DISK_RAID_LAYOUT:-[]}" != "[]" ]; then
    mapfile -t data_drives < <(sasircu_data_drives "$bin" "$ctrl")
    while IFS=$'\t' read -r layout_name layout_level layout_count; do
      [ -n "$layout_name" ] || continue
      drives_csv="$(join_first_n_csv "$layout_count" "${data_drives[@]}")"
      if [ "$(csv_count "$drives_csv")" -lt "$layout_count" ]; then
        echo "WARN: controller $ctrl data layout ${layout_name} requires ${layout_count} drives, only $(csv_count "$drives_csv") available"
        continue
      fi
      if [ "$layout_count" -ne 2 ]; then
        echo "WARN: sasircu data layout currently supports disk_count=2 only, ${layout_name} requested=${layout_count}"
        continue
      fi
      sas_level="$(sasircu_raid_level "$layout_level")"
      echo "==== SASIRCU CTRL $ctrl DATA ${layout_name} ${sas_level} ${drives_csv} ===="
      if "$bin" "$ctrl" CREATE "$sas_level" MAX "${data_drives[0]}" "${data_drives[1]}" "$layout_name" NOPROMPT; then
        data_drives=("${data_drives[@]:$layout_count}")
      fi
    done < <(data_layout_rows)
  fi

  echo "==== SASIRCU CTRL $ctrl CREATED $created ===="
  "$bin" "$ctrl" DISPLAY || true
}

detect_and_prepare_tools() {
  local pci
  pci="$(lspci -nn || true)"

  if printf '%s\n' "$pci" | grep -Eqi 'MegaRAID|Broadcom / LSI MegaRAID|9560|95[0-9]{2}|SAS39'; then
    ensure_storcli
    if [ -z "$STORCLI" ]; then
      ensure_megacli
    fi
  fi

  if printf '%s\n' "$pci" | grep -Eqi 'SAS3|SAS3008|SAS31|LSI.*3008|Avago.*3008'; then
    ensure_sas3ircu
  fi

  if printf '%s\n' "$pci" | grep -Eqi 'SAS2|SAS2008|SAS2308|LSI.*2008|LSI.*2308'; then
    ensure_sas2ircu
  fi
}

run_raid_config() {
  local did_any=0

  if [ -n "$STORCLI" ]; then
    mapfile -t ctrls < <(storcli_ctrls)
    if [ "${#ctrls[@]}" -gt 0 ]; then
      for ctrl in "${ctrls[@]}"; do
        configure_with_storcli "$ctrl"
        did_any=1
      done
    fi
  fi

  if [ "$did_any" -eq 0 ] && [ -n "$MEGACLI" ]; then
    mapfile -t ctrls < <(megacli_ctrls)
    if [ "${#ctrls[@]}" -gt 0 ]; then
      for ctrl in "${ctrls[@]}"; do
        configure_with_megacli "$ctrl"
        did_any=1
      done
    fi
  fi

  if [ "$did_any" -eq 0 ] && [ -n "$SAS3IRCU" ]; then
    mapfile -t ctrls < <(sasircu_ctrls "$SAS3IRCU")
    if [ "${#ctrls[@]}" -gt 0 ]; then
      for ctrl in "${ctrls[@]}"; do
        configure_with_sasircu "$SAS3IRCU" "$ctrl"
        did_any=1
      done
    fi
  fi

  if [ "$did_any" -eq 0 ] && [ -n "$SAS2IRCU" ]; then
    mapfile -t ctrls < <(sasircu_ctrls "$SAS2IRCU")
    if [ "${#ctrls[@]}" -gt 0 ]; then
      for ctrl in "${ctrls[@]}"; do
        configure_with_sasircu "$SAS2IRCU" "$ctrl"
        did_any=1
      done
    fi
  fi

  echo "==== RAID CONFIG EXECUTED: $did_any ===="
}

echo "==== START $(date) ===="
lsblk -d -o NAME,SIZE,TYPE,MODEL,SERIAL || true
apply_node_config_overrides
detect_and_prepare_tools
print_tools
run_raid_config

udevadm settle || true
partprobe || true
sleep 8

echo "==== AFTER RAID CONFIG ===="
lsblk -d -o NAME,SIZE,TYPE,MODEL,SERIAL || true

wipe_visible_disks

echo "==== AFTER WIPE ===="
lsblk -d -o NAME,SIZE,TYPE,MODEL,SERIAL || true

if [ -n "$STORCLI" ]; then
  mapfile -t ctrls < <(storcli_ctrls)
  for ctrl in "${ctrls[@]}"; do
    "$STORCLI" "/c$ctrl" /vall show || true
    "$STORCLI" "/c$ctrl" show bootdrive || true
  done
fi

if [ -n "$MEGACLI" ]; then
  mapfile -t ctrls < <(megacli_ctrls)
  for ctrl in "${ctrls[@]}"; do
    "$MEGACLI" -LDInfo -Lall -a"$ctrl" || true
  done
fi

if [ -n "$SAS3IRCU" ]; then
  mapfile -t ctrls < <(sasircu_ctrls "$SAS3IRCU")
  for ctrl in "${ctrls[@]}"; do
    "$SAS3IRCU" "$ctrl" DISPLAY || true
  done
fi

if [ -n "$SAS2IRCU" ]; then
  mapfile -t ctrls < <(sasircu_ctrls "$SAS2IRCU")
  for ctrl in "${ctrls[@]}"; do
    "$SAS2IRCU" "$ctrl" DISPLAY || true
  done
fi

echo "==== DONE $(date) ===="
