#!/bin/bash

set -euo pipefail

VERSION="${VERSION:-}"
OFFLINE_ROOT="${OFFLINE_ROOT:-/srv/maas-offline}"
TOOLKIT_ROOT="${TOOLKIT_ROOT:-/home/ubuntu/MAAS-offline-toolkit}"
OUTPUT_DIR="${OUTPUT_DIR:-/home/ubuntu/releases}"

if [ -z "$VERSION" ]; then
  echo "VERSION is required, for example VERSION=2026.08.11-01" >&2
  exit 2
fi

for path in "$OFFLINE_ROOT" "$TOOLKIT_ROOT"; do
  if [ ! -d "$path" ]; then
    echo "required directory not found: $path" >&2
    exit 2
  fi
done

for command in rsync sha256sum tar zstd; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "required command not found: $command" >&2
    exit 2
  fi
done

release_name="maas-offline-${VERSION}"
mkdir -p "$OUTPUT_DIR"
stage_dir="$(mktemp -d "${OUTPUT_DIR}/.${release_name}.XXXXXX")"
release_root="${stage_dir}/${release_name}"
archive="${OUTPUT_DIR}/${release_name}.tar.zst"

cleanup() {
  case "$stage_dir" in
    "${OUTPUT_DIR}/.${release_name}."*)
      if [ -d "$stage_dir" ]; then
        chmod -R u+w "$stage_dir" 2>/dev/null || true
        rm -rf "$stage_dir"
      fi
      ;;
  esac
}
trap cleanup EXIT

mkdir -p "$release_root/resources" "$release_root/toolkit"

rsync -a --exclude='.DS_Store' --exclude='._*' \
  "$OFFLINE_ROOT/" "$release_root/resources/"
rsync -a \
  --exclude='.git/' \
  --exclude='.DS_Store' \
  --exclude='._*' \
  --exclude='ai.txt' \
  --exclude='__pycache__/' \
  --exclude='*.pyc' \
  --exclude='.tmp/' \
  --exclude='.hotfix-backup-*/' \
  "$TOOLKIT_ROOT/" "$release_root/toolkit/"

# A release contains reusable resources and code, never the source environment's
# node inventory, reports, exports, credentials, or console runtime state.
rm -rf "$release_root/resources/stage1/output" "$release_root/resources/stage1/export"
mkdir -p "$release_root/resources/stage1/output" "$release_root/resources/stage1/export"
printf 'hostname,bmc_ip,bmc_user,bmc_pass,sn,25g,25g_mode,tag\n' > "$release_root/resources/stage1/inventory.csv"
printf '{"reports":{}}\n' > "$release_root/resources/stage1/state.json"
if [ -f "$release_root/toolkit/docs/stage1/defaults.example.yaml" ]; then
  cp "$release_root/toolkit/docs/stage1/defaults.example.yaml" \
    "$release_root/resources/stage1/defaults.yaml"
fi
rm -rf "$release_root/toolkit/.tmp"
rm -f "$release_root/toolkit/docs/lab/two-node-physical.local.json"
if [ -f "$release_root/toolkit/docs/lab/two-node-physical.example.json" ]; then
  cp "$release_root/toolkit/docs/lab/two-node-physical.example.json" \
    "$release_root/toolkit/docs/lab/two-node-physical.local.json"
fi

# Never ship an example client as live configuration.  The console treats the
# uploaded CSV as the authoritative inventory and starts empty on a new site.
python3 - "$release_root/toolkit/docs/lab/two-node-physical.local.json" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
if path.exists():
    config = json.loads(path.read_text(encoding="utf-8"))
    config["clients"] = []
    config.setdefault("inventory", {})["uploaded_csv"] = ""
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

cat > "$release_root/RELEASE_INFO.txt" <<EOF
release_version=${VERSION}
release_name=${release_name}
created_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)
deployment_image=Ubuntu 22.04.4 / jammy (existing MAAS boot resource)
offline_resources=resources/
toolkit=toolkit/

Restore on a new Ubuntu 22.04 control node:
  sudo mkdir -p /srv/maas-offline /opt/maas-offline-toolkit
  sudo rsync -a resources/ /srv/maas-offline/
  sudo rsync -a toolkit/ /opt/maas-offline-toolkit/
  cd /opt/maas-offline-toolkit
  sudo ./docs/maas-control-plane-oneclick.sh --help

Notes:
  - This release contains the existing Ubuntu 22.04.4 MAAS/Stage1 resources.
  - Runtime inventories, reports, exports, console state and live credentials are reset.
  - ai.txt, Git metadata, bytecode caches and macOS metadata are excluded.
EOF

(
  cd "$release_root"
  find . -type f ! -name SHA256SUMS -print0 \
    | sort -z \
    | xargs -0 sha256sum > SHA256SUMS
)

rm -f "${archive}.partial"
tar -I 'zstd -T0 -3' -cf "${archive}.partial" -C "$stage_dir" "$release_name"
mv "${archive}.partial" "$archive"
(
  cd "$OUTPUT_DIR"
  sha256sum "${release_name}.tar.zst" > "${release_name}.tar.zst.sha256"
)

echo "release_archive=$archive"
echo "release_checksum=${archive}.sha256"
du -h "$archive" "${archive}.sha256"
