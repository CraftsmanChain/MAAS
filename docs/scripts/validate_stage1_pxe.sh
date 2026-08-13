#!/bin/bash

set -euo pipefail

OFFLINE_ROOT=/srv/maas-offline
HTTP_PORT=8083
STAGE1_PORT=8091
CONFIG_DIR=/etc/maas-offline
SYSTEMD_DIR=/etc/systemd/system
IPXE_DIR=/usr/lib/ipxe
CHECK_SYSTEMD=1

errors=()
warnings=()
infos=()

usage() {
  cat <<'EOF'
Usage: validate_stage1_pxe.sh [options]

Options:
  --offline-root PATH     Offline root path. Default: /srv/maas-offline
  --http-port PORT        HTTP port used by maas-offline-http.service. Default: 8083
  --stage1-port PORT      Stage1 collector port. Default: 8091
  --config-dir PATH       Stage1 config directory. Default: /etc/maas-offline
  --systemd-dir PATH      systemd unit directory. Default: /etc/systemd/system
  --ipxe-dir PATH         Source iPXE binary directory. Default: /usr/lib/ipxe
  --skip-systemd-checks   Skip unit-file and systemctl checks.
  -h, --help              Show this help.
EOF
}

add_error() {
  errors+=("$1")
}

add_warning() {
  warnings+=("$1")
}

add_info() {
  infos+=("$1")
}

check_file() {
  local path="$1"
  local message="$2"
  if [ -f "$path" ]; then
    add_info "OK: ${path}"
  else
    add_error "${message}: ${path}"
  fi
}

check_dir() {
  local path="$1"
  local message="$2"
  if [ -d "$path" ]; then
    add_info "OK: ${path}"
  else
    add_error "${message}: ${path}"
  fi
}

check_contains() {
  local path="$1"
  local needle="$2"
  local message="$3"
  if [ ! -f "$path" ]; then
    add_error "${message}: ${path}"
    return
  fi
  if grep -Fq "$needle" "$path"; then
    add_info "OK: ${path} contains ${needle}"
  else
    add_error "${message}: ${path} missing ${needle}"
  fi
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --offline-root) OFFLINE_ROOT="${2:-}"; shift 2 ;;
    --http-port) HTTP_PORT="${2:-}"; shift 2 ;;
    --stage1-port) STAGE1_PORT="${2:-}"; shift 2 ;;
    --config-dir) CONFIG_DIR="${2:-}"; shift 2 ;;
    --systemd-dir) SYSTEMD_DIR="${2:-}"; shift 2 ;;
    --ipxe-dir) IPXE_DIR="${2:-}"; shift 2 ;;
    --skip-systemd-checks) CHECK_SYSTEMD=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage; exit 2 ;;
  esac
done

DISKLESS_DIR="${OFFLINE_ROOT}/diskless/ubuntu-22.04"
TFTP_DIR="${OFFLINE_ROOT}/diskless/tftp"
STAGE1_DIR="${OFFLINE_ROOT}/stage1"
DNSMASQ_CONF="${CONFIG_DIR}/diskless-stage1-dnsmasq.conf"
COLLECTOR_ENV="${CONFIG_DIR}/stage1-collector.env"
ISO_DIR="${OFFLINE_ROOT}/iso"

find_stage1_iso() {
  local candidate
  while IFS= read -r candidate; do
    [ -n "$candidate" ] || continue
    case "$(basename "$candidate")" in
      ._*) continue ;;
    esac
    printf '%s\n' "$candidate"
    return 0
  done < <(find "$ISO_DIR" -maxdepth 1 -type f -name 'ubuntu-22.04*live-server-amd64.iso' 2>/dev/null | sort)
  return 1
}

check_dir "$OFFLINE_ROOT" "缺少 offline_root 目录"
check_dir "$DISKLESS_DIR" "缺少 Stage1 无盘目录"
check_dir "$TFTP_DIR" "缺少 TFTP 根目录"
check_dir "$STAGE1_DIR" "缺少 Stage1 状态目录"

check_file "${DISKLESS_DIR}/vmlinuz" "缺少无盘启动内核"
check_file "${DISKLESS_DIR}/initrd" "缺少无盘启动 initrd"
check_file "${DISKLESS_DIR}/stage1.ipxe" "缺少 Stage1 iPXE 脚本"
check_file "${DISKLESS_DIR}/nocloud/meta-data" "缺少 Stage1 NoCloud meta-data"
check_file "${DISKLESS_DIR}/nocloud/user-data" "缺少 Stage1 NoCloud user-data"
check_file "${DISKLESS_DIR}/nocloud/vendor-data" "缺少 Stage1 NoCloud vendor-data，cloud-init 可能反复重试该文件"
check_file "${DISKLESS_DIR}/nocloud/stage1-report.py" "缺少 Stage1 节点侧采集脚本"
check_contains "${DISKLESS_DIR}/stage1.ipxe" "initrd=initrd" "Stage1 iPXE kernel 参数缺少 initrd=initrd，可能导致内核无法挂载 rootfs"
check_contains "${DISKLESS_DIR}/stage1.ipxe" "next-server" "Stage1 iPXE 未按 DHCP next-server 动态选择 PXE 网卡，多网卡机器可能拿错网卡"
check_contains "${DISKLESS_DIR}/stage1.ipxe" "BOOTIF=01-\${boot-mac}" "Stage1 iPXE kernel 参数缺少动态 BOOTIF，live ISO 可能在多网卡机器上改走管理网"
check_contains "${DISKLESS_DIR}/stage1.ipxe" "ip=\${boot-ip}:" "Stage1 iPXE kernel 参数没有传递 iPXE 已获得的静态地址，多网卡机器可能二次 DHCP 失败"
check_contains "${DISKLESS_DIR}/stage1.ipxe" "initrd --name initrd" "Stage1 iPXE initrd 未命名为 initrd，部分 iPXE/UEFI 组合下内核不会关联 initramfs"
check_contains "${DISKLESS_DIR}/stage1.ipxe" "netboot=url" "Stage1 iPXE 必须使用 Ubuntu casper 支持的 netboot=url 模式"
check_contains "${DISKLESS_DIR}/stage1.ipxe" "url=http://" "Stage1 iPXE 缺少 Ubuntu live-server ISO URL"
check_contains "${DISKLESS_DIR}/stage1.ipxe" "autoinstall" "Stage1 iPXE 缺少 autoinstall 参数，会进入 Ubuntu 安装器交互界面"
check_contains "${DISKLESS_DIR}/stage1.ipxe" "ds=nocloud-net;s=" "Stage1 iPXE 缺少 NoCloud 数据源，节点不会自动上报采集结果"
check_contains "${DISKLESS_DIR}/nocloud/user-data" "early-commands" "Stage1 NoCloud user-data 必须在安装器早期阶段执行采集"
check_contains "${DISKLESS_DIR}/nocloud/user-data" "ipmitool" "Stage1 NoCloud user-data 缺少离线 ipmitool 安装"
check_contains "${DISKLESS_DIR}/nocloud/user-data" "poweroff" "Stage1 NoCloud user-data 必须在采集完成后停止，避免继续进入安装流程"
check_contains "${DISKLESS_DIR}/nocloud/stage1-report.py" "/api/v1/report" "Stage1 节点侧采集脚本缺少上报逻辑"
check_contains "${DISKLESS_DIR}/nocloud/stage1-report.py" "local-ipmi-kcs" "Stage1 节点侧采集脚本缺少本机 IPMI/KCS 配置实现"
check_contains "${DISKLESS_DIR}/nocloud/stage1-report.py" "bmc_readback_ok" "Stage1 节点侧采集脚本缺少 BMC 回读校验"
check_contains "${DISKLESS_DIR}/nocloud/stage1-report.py" "bmc_user_readback_ok" "Stage1 节点侧采集脚本缺少 BMC 用户回读校验"
check_contains "${DISKLESS_DIR}/nocloud/stage1-report.py" "channel\", \"getaccess" "Stage1 节点侧采集脚本缺少 BMC channel 权限回读"
stage1_iso="$(find_stage1_iso || true)"
if [ -n "$stage1_iso" ]; then
  add_info "OK: ${stage1_iso}"
else
  add_error "缺少 Ubuntu 22.04 live-server ISO: ${ISO_DIR}/ubuntu-22.04*live-server-amd64.iso"
fi
if [ -f "${DISKLESS_DIR}/rootfs.squashfs" ]; then
  add_info "OK: ${DISKLESS_DIR}/rootfs.squashfs"
else
  add_warning "未发现 ${DISKLESS_DIR}/rootfs.squashfs；当前 Stage1 使用 ISO URL 启动，不再强依赖该文件"
fi
check_file "${TFTP_DIR}/ipxe.efi" "缺少 UEFI iPXE 引导文件"
check_file "${TFTP_DIR}/ipxe.efi.source" "缺少 UEFI iPXE 源文件标记"
check_file "${TFTP_DIR}/undionly.kpxe" "缺少 Legacy iPXE 引导文件"
check_file "${STAGE1_DIR}/inventory.csv" "缺少 Stage1 inventory.csv"
check_file "${STAGE1_DIR}/defaults.yaml" "缺少 Stage1 defaults.yaml"
check_file "${STAGE1_DIR}/state.json" "缺少 Stage1 state.json"

if [ -f "${TFTP_DIR}/ipxe.efi.source" ]; then
  uefi_ipxe_source="$(tr -d '[:space:]' < "${TFTP_DIR}/ipxe.efi.source")"
  if [ -n "$uefi_ipxe_source" ]; then
    add_info "UEFI iPXE source: ${uefi_ipxe_source}"
  fi
  if [ -n "$uefi_ipxe_source" ] && [ -f "${IPXE_DIR}/${uefi_ipxe_source}" ] && [ -f "${TFTP_DIR}/ipxe.efi" ]; then
    expected_hash="$(shasum -a 256 "${IPXE_DIR}/${uefi_ipxe_source}" | awk '{print $1}')"
    deployed_hash="$(shasum -a 256 "${TFTP_DIR}/ipxe.efi" | awk '{print $1}')"
    if [ "$expected_hash" = "$deployed_hash" ]; then
      add_info "UEFI iPXE binary matches ${uefi_ipxe_source}"
    else
      add_error "UEFI iPXE 文件内容与源文件标记不一致: expected=${uefi_ipxe_source}"
    fi
  fi
  if [ "$uefi_ipxe_source" = "snponly.efi" ]; then
    add_warning "当前 UEFI iPXE 源文件为 snponly.efi；这是兼容性回退模式。若仍在 UEFI pre-boot 阶段崩溃，可再切回 ipxe.efi 对比"
  elif [ "$uefi_ipxe_source" = "ipxe.efi" ]; then
    add_info "当前 UEFI iPXE 源文件为 ipxe.efi；若服务器在 ipxe.efi 阶段出现 UEFI General Protection Fault，可切换到 snponly.efi"
  fi
fi

if [ -d "$IPXE_DIR" ]; then
  add_info "OK: ${IPXE_DIR}"
else
  add_warning "未发现 iPXE 源目录 ${IPXE_DIR}；若后续重新执行部署脚本，可能无法自动补齐 ipxe.efi/undionly.kpxe"
fi

check_file "$DNSMASQ_CONF" "缺少 dnsmasq 配置文件"
check_contains "$DNSMASQ_CONF" "tftp-root=${TFTP_DIR}" "dnsmasq 配置缺少正确的 tftp-root"
check_contains "$DNSMASQ_CONF" "dhcp-boot=tag:efi-x86_64,ipxe.efi" "dnsmasq 配置缺少 UEFI iPXE 引导项"
check_contains "$DNSMASQ_CONF" "dhcp-boot=undionly.kpxe" "dnsmasq 配置缺少 Legacy iPXE 引导项"
check_contains "$DNSMASQ_CONF" "/diskless/ubuntu-22.04/stage1.ipxe" "dnsmasq 配置缺少 Stage1 iPXE HTTP 引导地址"
if [ -f "$DNSMASQ_CONF" ]; then
  check_contains "$DNSMASQ_CONF" "interface=" "dnsmasq 配置缺少 PXE 网卡"
  check_contains "$DNSMASQ_CONF" "dhcp-range=" "dnsmasq 配置缺少 DHCP 地址池"
  if command -v dnsmasq >/dev/null 2>&1; then
    if dnsmasq --test --conf-file="$DNSMASQ_CONF" >/dev/null 2>&1; then
      add_info "OK: dnsmasq configuration syntax"
    else
      add_error "dnsmasq 配置语法校验失败: ${DNSMASQ_CONF}"
    fi
  else
    add_error "缺少 dnsmasq，无法启动 Stage1 DHCP/TFTP"
  fi
  ipxe_boot_line="$(grep -nF "dhcp-boot=tag:ipxe,http://" "$DNSMASQ_CONF" | tail -1 | cut -d: -f1 || true)"
  efi_boot_line="$(grep -nF "dhcp-boot=tag:efi-x86_64,ipxe.efi" "$DNSMASQ_CONF" | tail -1 | cut -d: -f1 || true)"
  legacy_boot_line="$(grep -nF "dhcp-boot=undionly.kpxe" "$DNSMASQ_CONF" | tail -1 | cut -d: -f1 || true)"
  if [ -n "$ipxe_boot_line" ] && [ -n "$efi_boot_line" ] && [ "$ipxe_boot_line" -gt "$efi_boot_line" ]; then
    add_info "OK: iPXE HTTP boot rule is after UEFI boot rule"
  elif [ -n "$ipxe_boot_line" ] && [ -n "$efi_boot_line" ]; then
    add_error "dnsmasq 中 tag:ipxe 的 HTTP 引导项必须排在 UEFI ipxe.efi 引导项之后，否则 iPXE 二阶段会被覆盖"
  fi
  if [ -n "$ipxe_boot_line" ] && [ -n "$legacy_boot_line" ] && [ "$ipxe_boot_line" -gt "$legacy_boot_line" ]; then
    add_info "OK: iPXE HTTP boot rule is after Legacy boot rule"
  elif [ -n "$ipxe_boot_line" ] && [ -n "$legacy_boot_line" ]; then
    add_error "dnsmasq 中 tag:ipxe 的 HTTP 引导项必须排在 Legacy undionly.kpxe 引导项之后，否则 iPXE 二阶段会被覆盖"
  fi
fi

check_file "$COLLECTOR_ENV" "缺少 Stage1 collector 环境文件"
check_contains "$COLLECTOR_ENV" "STAGE1_INVENTORY=${STAGE1_DIR}/inventory.csv" "collector 环境缺少 inventory 路径"
check_contains "$COLLECTOR_ENV" "STAGE1_DEFAULTS=${STAGE1_DIR}/defaults.yaml" "collector 环境缺少 defaults 路径"
check_contains "$COLLECTOR_ENV" "STAGE1_STATE=${STAGE1_DIR}/state.json" "collector 环境缺少 state 路径"
check_contains "$COLLECTOR_ENV" "STAGE1_PORT=${STAGE1_PORT}" "collector 环境缺少正确的 Stage1 端口"

if [ "$CHECK_SYSTEMD" -eq 1 ]; then
  if command -v ipmitool >/dev/null 2>&1; then
    add_info "OK: collector ipmitool is installed"
  else
    add_error "collector 缺少 ipmitool，无法从服务端验证 BMC 账号"
  fi
  check_file "${SYSTEMD_DIR}/maas-offline-http.service" "缺少 maas-offline-http systemd unit 文件"
  check_file "${SYSTEMD_DIR}/stage1-collector.service" "缺少 stage1-collector systemd unit 文件"
  check_file "${SYSTEMD_DIR}/diskless-stage1-dnsmasq.service" "缺少 diskless-stage1-dnsmasq systemd unit 文件"

  if command -v systemctl >/dev/null 2>&1; then
    for unit in maas-offline-http.service stage1-collector.service diskless-stage1-dnsmasq.service; do
      state="$(systemctl is-active "$unit" 2>/dev/null || true)"
      if [ -n "$state" ]; then
        add_info "systemctl: ${unit} is ${state}"
      else
        add_warning "systemctl 无法读取 ${unit} 当前状态"
      fi
    done
  else
    add_warning "当前环境没有 systemctl，已跳过服务状态检查"
  fi
fi

echo "Stage1 PXE 校验结果"
echo "offline_root=${OFFLINE_ROOT}"
echo "http_port=${HTTP_PORT}"
echo "stage1_port=${STAGE1_PORT}"

if [ "${#infos[@]}" -gt 0 ]; then
  echo
  echo "[通过项]"
  for item in "${infos[@]}"; do
    echo "- ${item}"
  done
fi

if [ "${#warnings[@]}" -gt 0 ]; then
  echo
  echo "[告警]"
  for item in "${warnings[@]}"; do
    echo "- ${item}"
  done
fi

if [ "${#errors[@]}" -gt 0 ]; then
  echo
  echo "[失败项]"
  for item in "${errors[@]}"; do
    echo "- ${item}"
  done
  echo
  echo "建议动作："
  echo "- 先补齐 ${OFFLINE_ROOT}/diskless 和 ${OFFLINE_ROOT}/stage1 下的无盘资源、TFTP 资源和状态文件"
  echo "- 若 TFTP 目录缺少 ipxe.efi / undionly.kpxe，请确认控制机已安装 iPXE 相关文件，再重新执行 docs/diskless-stage1-oneclick.sh"
  echo "- 确认 /etc/maas-offline/diskless-stage1-dnsmasq.conf 中的 tftp-root、dhcp-boot 和 Stage1 HTTP 地址与当前配置一致"
  echo "- 校验通过前不要切到 diskless_stage1，避免节点再次卡在 tftp://.../ipxe.efi 超时"
  exit 1
fi

echo
echo "stage1_pxe_valid=true"
