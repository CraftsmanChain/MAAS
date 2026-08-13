#!/bin/bash

set -euo pipefail

kernel_package="linux-image-5.15.0-78-generic"

mkdir -p /curtin
printf '%s\n' "$kernel_package" > /curtin/CUSTOM_KERNEL

cat >> /curtin/install-custom-packages <<'EOF'

installed="$(dpkg-query -W -f='${Status}' linux-image-5.15.0-78-generic 2>/dev/null || true)"
if [ "$installed" != "install ok installed" ]; then
  echo "linux-image-5.15.0-78-generic was not installed" >&2
  exit 1
fi

apt-mark hold \
  linux-image-5.15.0-78-generic \
  linux-modules-5.15.0-78-generic \
  linux-modules-extra-5.15.0-78-generic \
  linux-headers-5.15.0-78 \
  linux-headers-5.15.0-78-generic
EOF
