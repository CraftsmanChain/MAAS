#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
SOURCES_DIR="${1:-$(cd "${PROJECT_DIR}/.." && pwd)/MAAS-sources}"
OUT_DIR="${SOURCES_DIR}/tools/maas-control-repo"
PLATFORM="${PLATFORM:-linux/amd64}"
IMAGE="${IMAGE:-ubuntu:22.04}"
PPA_URL="${PPA_URL:-http://ppa.launchpadcontent.net/maas/3.4/ubuntu}"
SERIES="${SERIES:-jammy}"
PACKAGES=(
  maas-region-api
  maas-rack-controller
  maas-cli
  postgresql
  postgresql-contrib
  cloud-init
  curtin-common
  python3-yaml
  lsb-release
  dnsutils
  bind9-dnsutils
  jq
  curl
)

mkdir -p "$OUT_DIR"

docker run --rm \
  --platform "$PLATFORM" \
  -v "${OUT_DIR}:/out" \
  "$IMAGE" \
  bash -lc '
    set -euo pipefail
    export DEBIAN_FRONTEND=noninteractive
    apt_retry=(-o Acquire::Retries=5 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30)
    series="'"$SERIES"'"
    ppa_url="'"$PPA_URL"'"
    packages=('"${PACKAGES[*]}"')

    printf "deb [trusted=yes] %s %s main\n" "$ppa_url" "$series" >/etc/apt/sources.list.d/maas-control.list
    apt-get "${apt_retry[@]}" update
    apt-get "${apt_retry[@]}" install -y --download-only --no-install-recommends "${packages[@]}"

    rm -rf /out/pool /out/dists
    mkdir -p /out/pool/main /out/dists/'"$SERIES"'/main/binary-amd64
    cp -a /var/cache/apt/archives/*.deb /out/pool/main/

    cd /out
    : > dists/'"$SERIES"'/main/binary-amd64/Packages
    for deb in pool/main/*.deb; do
      [ -f "$deb" ] || continue
      size="$(stat -c %s "$deb")"
      md5="$(md5sum "$deb" | awk "{print \$1}")"
      sha1="$(sha1sum "$deb" | awk "{print \$1}")"
      sha256="$(sha256sum "$deb" | awk "{print \$1}")"
      dpkg-deb -f "$deb" >> dists/'"$SERIES"'/main/binary-amd64/Packages
      {
        printf "Filename: %s\n" "$deb"
        printf "Size: %s\n" "$size"
        printf "MD5sum: %s\n" "$md5"
        printf "SHA1: %s\n" "$sha1"
        printf "SHA256: %s\n\n" "$sha256"
      } >> dists/'"$SERIES"'/main/binary-amd64/Packages
    done
    gzip -c dists/'"$SERIES"'/main/binary-amd64/Packages > dists/'"$SERIES"'/main/binary-amd64/Packages.gz
    packages_path=main/binary-amd64/Packages
    packages_gz_path=main/binary-amd64/Packages.gz
    packages_file=dists/'"$SERIES"'/$packages_path
    packages_gz_file=dists/'"$SERIES"'/$packages_gz_path
    packages_size="$(stat -c %s "$packages_file")"
    packages_gz_size="$(stat -c %s "$packages_gz_file")"
    cat > dists/'"$SERIES"'/Release <<EOF
Origin: MAAS Offline Control Repo
Label: MAAS Offline Control Repo
Suite: '"$SERIES"'
Codename: '"$SERIES"'
Date: $(date -Ru)
Architectures: amd64
Components: main
Description: Offline MAAS control-plane deb repository
MD5Sum:
 $(md5sum "$packages_file" | awk "{print \$1}") ${packages_size} ${packages_path}
 $(md5sum "$packages_gz_file" | awk "{print \$1}") ${packages_gz_size} ${packages_gz_path}
SHA1:
 $(sha1sum "$packages_file" | awk "{print \$1}") ${packages_size} ${packages_path}
 $(sha1sum "$packages_gz_file" | awk "{print \$1}") ${packages_gz_size} ${packages_gz_path}
SHA256:
 $(sha256sum "$packages_file" | awk "{print \$1}") ${packages_size} ${packages_path}
 $(sha256sum "$packages_gz_file" | awk "{print \$1}") ${packages_gz_size} ${packages_gz_path}
EOF
    printf "maas_control_repo_packages=%s\n" "$(find /out/pool/main -name "*.deb" | wc -l)"
  '

cat <<EOF
maas_control_repo_ready=true
repo_dir=${OUT_DIR}
repo_url=/tools/maas-control-repo
EOF
