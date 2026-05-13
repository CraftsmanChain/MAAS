#!/bin/bash
# --- Start MAAS 1.0 script metadata ---
# name: wipe-raid-and-disks-test
# title: wipe-raid-and-disks-test
# description: wipe-raid-and-disks-test
# script_type: testing
# timeout: 00:30:00
# parallel: disabled
# --- End MAAS 1.0 script metadata ---

set -euxo pipefail
exec > >(tee /tmp/wipe-raid-and-disks.log) 2>&1

BASE_URL="http://10.161.139.136:8083"
WORKDIR="/tmp/raid-tools"
mkdir -p "$WORKDIR"

STORCLI=""
MEGACLI=""
SAS3IRCU=""
SAS2IRCU=""

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
  echo "STORCLI=${STORCLI:-}"
  echo "MEGACLI=${MEGACLI:-}"
  echo "SAS3IRCU=${SAS3IRCU:-}"
  echo "SAS2IRCU=${SAS2IRCU:-}"
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

  local idx=0
  local created=0
  local boot_vd=""

  for drive in "${ssds[@]}"; do
    [ -n "$drive" ] || continue
    local name
    name="$(printf 'ssd%02d' "$((idx + 1))")"
    if "$STORCLI" "/c$ctrl" add vd r0 name="$name" drives="$drive"; then
      created=$((created + 1))
    fi
    idx=$((idx + 1))
  done

  boot_vd="$(storcli_boot_vd_by_name "$ctrl" "ssd01" || true)"
  if [ -n "$boot_vd" ]; then
    "$STORCLI" "/c$ctrl/v$boot_vd" set bootdrive=on || true
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

  local idx=0
  local created=0
  local boot_ld=""

  for drive in "${ssds[@]}"; do
    [ -n "$drive" ] || continue
    if "$MEGACLI" -CfgLdAdd -r0["$drive"] WT NORA Direct -strpsz64 -a"$ctrl"; then
      created=$((created + 1))
    fi
    idx=$((idx + 1))
  done

  boot_ld="$(megacli_boot_ld_by_name "$ctrl" "ssd01" || true)"
  if [ -n "$boot_ld" ]; then
    "$MEGACLI" -AdpBootDrive -Set -L"$boot_ld" -a"$ctrl" || true
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

  local idx=0
  local created=0

  for drive in "${ssds[@]}"; do
    [ -n "$drive" ] || continue
    local name
    name="$(printf 'ssd%02d' "$((idx + 1))")"
    if "$bin" "$ctrl" CREATE RAID0 MAX "$drive" "$name" NOPROMPT; then
      created=$((created + 1))
    fi
    idx=$((idx + 1))
  done

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
