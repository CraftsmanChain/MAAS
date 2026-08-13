#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
DEFAULT_SOURCES_DIR="$(cd "${PROJECT_DIR}/.." && pwd)/MAAS-sources"
SOURCES_DIR="${1:-$DEFAULT_SOURCES_DIR}"

mkdir -p \
  "${SOURCES_DIR}/mirror" \
  "${SOURCES_DIR}/iso" \
  "${SOURCES_DIR}/tools/lldpd-mini-repo" \
  "${SOURCES_DIR}/diskless/ubuntu-22.04" \
  "${SOURCES_DIR}/diskless/tftp" \
  "${SOURCES_DIR}/stage1"

if [ ! -f "${SOURCES_DIR}/stage1/inventory.csv" ]; then
  cp "${PROJECT_DIR}/docs/stage1/inventory.example.csv" "${SOURCES_DIR}/stage1/inventory.csv"
fi

if [ ! -f "${SOURCES_DIR}/stage1/defaults.yaml" ]; then
  cp "${PROJECT_DIR}/docs/stage1/defaults.example.yaml" "${SOURCES_DIR}/stage1/defaults.yaml"
fi

if [ ! -f "${SOURCES_DIR}/stage1/state.json" ]; then
  printf '{"reports":{}}\n' >"${SOURCES_DIR}/stage1/state.json"
fi

cat >"${SOURCES_DIR}/README.md" <<'EOF'
# MAAS-sources

该目录用于保存 MAAS 离线部署资源，放在项目目录同级，不纳入代码仓库。

必需目录：

```text
MAAS-sources/
  mirror/
    ephemeral-v3/stable/streams/v1/index.sjson
  iso/
    dists/jammy/Release
    pool/...
  tools/
    lldpd-mini-repo/dists/jammy/Release
    storcli_007.2508.0000.0000_all.deb
    MegaCli64
    sas3ircu
    sas2ircu
  diskless/
    ubuntu-22.04/
      vmlinuz
      initrd
      rootfs.squashfs
      stage1.ipxe
    tftp/
      ipxe.efi
      undionly.kpxe
  stage1/
    inventory.csv
    defaults.yaml
    state.json
```

MAAS 控制端真实离线安装至少需要：

- Ubuntu 22.04 / jammy apt 仓库，包含 MAAS 及其依赖
- MAAS boot resources simplestreams mirror
- lldpd-mini-repo，并带 Release / InRelease 或 Release.gpg

没有上述资源时，一键脚本会在 resource check 或 apt install 阶段失败，这是预期保护。
EOF

cat <<EOF
maas_sources_ready=true
sources_dir=${SOURCES_DIR}
expected_mount=/srv/maas-offline
EOF
