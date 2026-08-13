#!/bin/bash

set -euo pipefail

DRY_RUN=0
OFFLINE_ROOT=/srv/maas-offline
HTTP_PORT=8083
SERVER_IP=""
MAAS_URL=""
PROFILE=admin
ADMIN_USER=admin
ADMIN_PASSWORD=""
ADMIN_EMAIL=admin@example.local
DB_URI=""
DB_NAME=maasdb
DB_USER=maas
DB_PASSWORD=""
SERIES=jammy
ARCH=amd64
SKIP_HTTP=0
SKIP_APT=0
SKIP_INSTALL=0
SKIP_INIT=0
SKIP_ADMIN=0
SKIP_LOGIN=0
SKIP_REPOS=0
SKIP_IMPORT=0
SKIP_RESOURCE_CHECK=0
INSTALL_CURTIN_TEMPLATE=0
POLICY_CONFIG=""
MACHINES_CSV=""
MAAS_CONTROL_REPO=""
MAAS_DHCP_INTERFACE=""
MAAS_DHCP_START_IP=""
MAAS_DHCP_END_IP=""
MAAS_DHCP_GATEWAY=""
MAAS_DHCP_DNS=""
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<'EOF'
Usage: maas-control-plane-oneclick.sh [options]

Install and configure the offline MAAS control plane for Ubuntu 22.04 node deployment.

Core options:
  --server-ip IP              Control-plane IP advertised to MAAS nodes.
  --maas-url URL              MAAS URL, default http://<server-ip>:5240/MAAS.
  --offline-root DIR          Offline root, default /srv/maas-offline.
  --http-port PORT            Offline HTTP port, default 8083.
  --profile NAME              MAAS CLI profile, default admin.
  --admin-user NAME           MAAS admin username, default admin.
  --admin-password PASS       MAAS admin password, required unless --skip-admin.
  --admin-email EMAIL         MAAS admin email, default admin@example.local.
  --db-uri URI                Optional PostgreSQL URI for maas init.
  --db-password PASS          PostgreSQL password for local MAAS DB user.
  --dry-run                   Print commands without changing the host.

Step control:
  --skip-http                 Do not run maas-offline-oneclick.sh.
  --skip-apt                  Do not write apt source or apt-get update.
  --skip-install              Do not install MAAS packages.
  --skip-init                 Do not run maas init.
  --skip-admin                Do not create admin user.
  --skip-login                Do not login MAAS CLI profile.
  --skip-repos                Do not configure boot-source/package repositories.
  --skip-import               Do not import boot resources.
  --skip-resource-check       Do not check offline resource paths.
  --maas-control-repo DIR     Optional MAAS deb repo path, default <offline-root>/tools/maas-control-repo.

Optional deploy integration:
  --install-curtin-template   Install curtin login template after MAAS setup.
  --policy-config PATH        Policy YAML, default docs/cloud-init/deploy-policy.yaml.
  --machines-csv PATH         Optional CSV for per-node curtin templates.

Optional DHCP enablement:
  --maas-dhcp-interface IF    Provisioning interface to enable in MAAS.
  --maas-dhcp-start IP        Dynamic range start IP for MAAS DHCP.
  --maas-dhcp-end IP          Dynamic range end IP for MAAS DHCP.
  --maas-dhcp-gateway IP      Provisioning subnet gateway written to MAAS.
  --maas-dhcp-dns IP          Provisioning subnet DNS server written to MAAS.

Expected offline resources:
  /srv/maas-offline/mirror/ephemeral-v3/stable/streams/v1/index.sjson
  /srv/maas-offline/iso/dists/jammy/Release
  /srv/maas-offline/tools/lldpd-mini-repo/dists/jammy/Release

Notes:
  This script targets Ubuntu 22.04 / jammy as the default deployment baseline.
EOF
}

log() {
  printf '[maas-control] %s\n' "$*"
}

run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '[dry-run] %q' "$1"
    shift || true
    for arg in "$@"; do
      printf ' %q' "$arg"
    done
    printf '\n'
    return 0
  fi
  "$@"
}

run_shell() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '[dry-run] %s\n' "$*"
    return 0
  fi
  bash -c "$*"
}

require_value() {
  local name="$1"
  local value="$2"
  if [ -z "$value" ]; then
    echo "missing required option: $name" >&2
    exit 2
  fi
}

detect_server_ip() {
  if [ -n "$SERVER_IP" ]; then
    return 0
  fi
  SERVER_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
  if [ -z "$SERVER_IP" ]; then
    SERVER_IP="127.0.0.1"
  fi
}

validate_maas_dhcp_options() {
  local configured=0
  local partial=0
  local value=""
  for value in "$MAAS_DHCP_INTERFACE" "$MAAS_DHCP_START_IP" "$MAAS_DHCP_END_IP"; do
    if [ -n "$value" ]; then
      configured=1
    else
      partial=1
    fi
  done

  if [ "$configured" -eq 1 ] && [ "$partial" -eq 1 ]; then
    echo "MAAS DHCP options must be provided together: --maas-dhcp-interface --maas-dhcp-start --maas-dhcp-end" >&2
    exit 2
  fi
}

resource_check() {
  local missing=0
  local paths=(
    "${OFFLINE_ROOT}/mirror/ephemeral-v3/stable/streams/v1/index.sjson"
    "${OFFLINE_ROOT}/iso/dists/${SERIES}/Release"
    "${OFFLINE_ROOT}/tools/lldpd-mini-repo/dists/${SERIES}/Release"
  )

  for path in "${paths[@]}"; do
    if [ "$DRY_RUN" -eq 1 ]; then
      log "would check resource: ${path}"
      continue
    fi
    if [ ! -e "$path" ]; then
      echo "missing offline resource: $path" >&2
      missing=1
    fi
  done

  if [ "$missing" -ne 0 ]; then
    echo "offline resource check failed" >&2
    exit 1
  fi

  if [ "$DRY_RUN" -eq 1 ]; then
    log "would check resource: ${MAAS_CONTROL_REPO}/dists/${SERIES}/main/binary-amd64/Packages.gz"
    return 0
  fi

  if [ ! -e "${MAAS_CONTROL_REPO}/dists/${SERIES}/main/binary-amd64/Packages.gz" ]; then
    echo "missing MAAS control-plane repo: ${MAAS_CONTROL_REPO}/dists/${SERIES}/main/binary-amd64/Packages.gz" >&2
    echo "build it with: ./docs/scripts/build_maas_control_repo.sh <MAAS-sources-dir>" >&2
    exit 1
  fi
}

write_apt_sources() {
  local list_file=/etc/apt/sources.list.d/maas-offline-jammy.list
  local disabled_dir=/etc/apt/sources.list.d/maas-offline-disabled
  local suites=()
  local suite

  for suite in "$SERIES" "${SERIES}-updates" "${SERIES}-security"; do
    if [ -f "${OFFLINE_ROOT}/iso/dists/${suite}/Release" ]; then
      suites+=("$suite")
    fi
  done

  if [ "${#suites[@]}" -eq 0 ] && [ "$DRY_RUN" -eq 1 ]; then
    suites=("$SERIES")
  elif [ "${#suites[@]}" -eq 0 ]; then
    echo "missing apt suite under ${OFFLINE_ROOT}/iso/dists: ${SERIES}" >&2
    exit 1
  fi

  if [ "$DRY_RUN" -eq 1 ]; then
    log "would disable default online apt sources under ${disabled_dir}"
    log "would write ${list_file}"
    log "would enable offline apt suites: ${suites[*]}"
  else
    sudo mkdir -p "$disabled_dir"
    sudo bash -c '
      shopt -s nullglob
      stamp="$(date +%Y%m%d%H%M%S)"
      disabled_dir="'"$disabled_dir"'"
      for file in /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do
        [ -f "$file" ] || continue
        base="$(basename "$file")"
        [ "$base" = "maas-offline-jammy.list" ] && continue
        case "$file" in
          "$disabled_dir"/*) continue ;;
        esac
        mv "$file" "$disabled_dir/${base}.${stamp}.disabled"
      done
    '
    sudo rm -rf /var/lib/apt/lists/*
    sudo mkdir -p /var/lib/apt/lists/partial
    : | sudo tee "$list_file" >/dev/null
    for suite in "${suites[@]}"; do
      local components
      components="$(awk '/^Components:/{for (i=2; i<=NF; i++) printf "%s%s", (i==2 ? "" : " "), $i}' "${OFFLINE_ROOT}/iso/dists/${suite}/Release")"
      if [ -z "$components" ]; then
        components="main"
      fi
      printf 'deb http://%s:%s/iso %s %s\n' "$SERVER_IP" "$HTTP_PORT" "$suite" "$components" | sudo tee -a "$list_file" >/dev/null
    done
    printf 'deb [trusted=yes] http://%s:%s/tools/maas-control-repo %s main\n' "$SERVER_IP" "$HTTP_PORT" "$SERIES" | sudo tee -a "$list_file" >/dev/null
  fi
  run sudo apt-get update
}

install_packages() {
  run sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y \
    postgresql \
    postgresql-contrib \
    maas-region-api \
    maas-rack-controller \
    maas-cli \
    cloud-init \
    curtin-common \
    python3-yaml \
    python3-paramiko \
    lsb-release \
    dnsutils \
    bind9-dnsutils \
    jq \
    curl

  if ! command -v nsupdate >/dev/null 2>&1; then
    echo "missing nsupdate after MAAS package installation; add dnsutils/bind9-dnsutils to the offline control repo" >&2
    exit 1
  fi

  local ansible_deb_dir="${OFFLINE_ROOT}/ansible/runtime/debs"
  if [ -d "$ansible_deb_dir" ] && compgen -G "${ansible_deb_dir}/*.deb" >/dev/null; then
    log "installing Ansible runtime from ${ansible_deb_dir}"
    run sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${ansible_deb_dir}"/*.deb
  else
    log "Ansible runtime not present; web console playbook execution will stay disabled"
  fi
}

read_existing_db_password() {
  if [ -n "$DB_PASSWORD" ] || [ ! -f /etc/maas/regiond.conf ]; then
    return 0
  fi
  DB_PASSWORD="$(awk -F': *' '$1 == "database_pass" {print $2; exit}' /etc/maas/regiond.conf 2>/dev/null || true)"
}

ensure_db_password() {
  read_existing_db_password
  if [ -z "$DB_PASSWORD" ]; then
    if command -v openssl >/dev/null 2>&1; then
      DB_PASSWORD="$(openssl rand -hex 24)"
    else
      DB_PASSWORD="maas-$(date +%s)"
    fi
  fi
}

prepare_postgresql() {
  ensure_db_password

  if [ "$DRY_RUN" -eq 1 ]; then
    log "would start PostgreSQL and ensure database ${DB_NAME}/${DB_USER}"
    log "would write database settings to /etc/maas/regiond.conf"
    return 0
  fi

  sudo systemctl enable postgresql

  # postgresql.service is only an umbrella unit on Ubuntu.  A partially
  # assembled offline repository can install the packages without creating a
  # versioned cluster, in which case the umbrella unit exits successfully but
  # no server owns /var/run/postgresql/.s.PGSQL.5432.
  if command -v pg_lsclusters >/dev/null 2>&1 && ! pg_lsclusters --no-header 2>/dev/null | awk '$3 == 5432 {found=1} END {exit !found}'; then
    local pg_version
    pg_version="$(ls -1 /usr/lib/postgresql 2>/dev/null | sort -V | tail -1)"
    if [ -z "$pg_version" ]; then
      echo "PostgreSQL packages are installed, but no server version was found under /usr/lib/postgresql" >&2
      exit 1
    fi
    log "creating missing PostgreSQL ${pg_version}/main cluster on port 5432"
    sudo pg_createcluster "$pg_version" main --start
  fi

  sudo systemctl start postgresql
  if command -v pg_ctlcluster >/dev/null 2>&1 && command -v pg_lsclusters >/dev/null 2>&1; then
    while read -r version name port status _; do
      [ "$port" = "5432" ] || continue
      [ "$status" = "online" ] || sudo pg_ctlcluster "$version" "$name" start
    done < <(pg_lsclusters --no-header 2>/dev/null || true)
  fi

  local postgres_ready=0
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30; do
    if sudo -u postgres pg_isready >/dev/null 2>&1; then
      postgres_ready=1
      break
    fi
    sleep 1
  done
  if [ "$postgres_ready" -ne 1 ]; then
    echo "PostgreSQL did not become ready on local port 5432" >&2
    command -v pg_lsclusters >/dev/null 2>&1 && pg_lsclusters >&2 || true
    sudo systemctl --no-pager --full status postgresql >&2 || true
    exit 1
  fi

  sudo -u postgres psql \
    -v ON_ERROR_STOP=1 \
    -v db_name="$DB_NAME" \
    -v db_user="$DB_USER" \
    -v db_password="$DB_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'db_user', :'db_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'db_user')\gexec
SELECT format('ALTER ROLE %I WITH LOGIN PASSWORD %L', :'db_user', :'db_password')
WHERE EXISTS (SELECT FROM pg_roles WHERE rolname = :'db_user')\gexec
SELECT format('CREATE DATABASE %I OWNER %I', :'db_name', :'db_user')
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'db_name')\gexec
SELECT format('ALTER DATABASE %I OWNER TO %I', :'db_name', :'db_user')\gexec
SQL

  sudo sed -i \
    -e '/^database_host:/d' \
    -e '/^database_port:/d' \
    -e '/^database_name:/d' \
    -e '/^database_user:/d' \
    -e '/^database_pass:/d' \
    /etc/maas/regiond.conf
  sudo tee -a /etc/maas/regiond.conf >/dev/null <<EOF
database_host: localhost
database_port: 5432
database_name: ${DB_NAME}
database_user: ${DB_USER}
database_pass: ${DB_PASSWORD}
EOF
}

upgrade_maas_database() {
  if [ "$DRY_RUN" -eq 1 ]; then
    log "would run MAAS database migrations with maas-region dbupgrade"
    return 0
  fi

  run sudo maas-region dbupgrade
}

init_maas() {
  local help_text=""
  if [ "$DRY_RUN" -eq 0 ]; then
    help_text="$(maas init --help 2>&1 || true)"
  fi

  if [ "$DRY_RUN" -eq 0 ] && grep -q -- "--maas-url" <<<"$help_text"; then
    local args=(init)
    args+=(region+rack --maas-url "$MAAS_URL")
    if [ -n "$DB_URI" ]; then
      args+=(--database-uri "$DB_URI")
    fi
    run sudo maas "${args[@]}"
  else
    if [ -n "$DB_URI" ]; then
      log "installed MAAS CLI does not support --database-uri; ignoring --db-uri"
    fi
    run sudo maas-region configauth \
      --rbac-url "" \
      --candid-agent-file "" \
      --candid-domain "" \
      --candid-admin-group ""
    run sudo maas-region local_config_set --maas-url "$MAAS_URL"
  fi
}

create_admin() {
  require_value "--admin-password" "$ADMIN_PASSWORD"
  if [ "$DRY_RUN" -eq 1 ]; then
    run sudo maas createadmin \
      --username "$ADMIN_USER" \
      --password "$ADMIN_PASSWORD" \
      --email "$ADMIN_EMAIL"
    return 0
  fi

  sudo env \
    MAAS_ADMIN_USER="$ADMIN_USER" \
    MAAS_ADMIN_PASSWORD="$ADMIN_PASSWORD" \
    MAAS_ADMIN_EMAIL="$ADMIN_EMAIL" \
    maas-region shell <<'PY'
import os
from django.contrib.auth import get_user_model

User = get_user_model()
user, _ = User.objects.get_or_create(username=os.environ["MAAS_ADMIN_USER"])
user.email = os.environ["MAAS_ADMIN_EMAIL"]
user.is_staff = True
user.is_superuser = True
user.set_password(os.environ["MAAS_ADMIN_PASSWORD"])
user.save()
PY
}

start_maas_services() {
  local api_version_url="${MAAS_URL%/}/api/2.0/version/"
  if [ "$DRY_RUN" -eq 1 ]; then
    log "would start MAAS API services and wait for ${api_version_url}"
    return 0
  fi

  sudo systemctl restart maas-http maas-regiond maas-rackd
  for _ in $(seq 1 90); do
    if curl -fsS "$api_version_url" 2>/dev/null | jq -e '.version' >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done

  sudo systemctl status maas-http maas-regiond maas-rackd --no-pager || true
  echo "MAAS API did not become ready: ${api_version_url}" >&2
  exit 1
}

login_profile() {
  local api_key_cmd
  api_key_cmd="sudo maas apikey --username ${ADMIN_USER}"
  if [ "$DRY_RUN" -eq 1 ]; then
    log "would create API key with: ${api_key_cmd}"
    run maas login "$PROFILE" "$MAAS_URL" "<api-key>"
    return 0
  fi
  local api_key
  api_key="$(sudo maas apikey --username "$ADMIN_USER")"
  maas login "$PROFILE" "$MAAS_URL" "$api_key"
}

ensure_maas_dhcp() {
  if [ -z "$MAAS_DHCP_INTERFACE" ]; then
    return 0
  fi

  local cmd=(
    sudo "$SCRIPT_DIR/scripts/maas_ensure_dhcp.sh"
    --profile "$PROFILE"
    --interface "$MAAS_DHCP_INTERFACE"
    --start-ip "$MAAS_DHCP_START_IP"
    --end-ip "$MAAS_DHCP_END_IP"
  )
  [ -z "$MAAS_DHCP_GATEWAY" ] || cmd+=(--gateway "$MAAS_DHCP_GATEWAY")
  [ -z "$MAAS_DHCP_DNS" ] || cmd+=(--dns "$MAAS_DHCP_DNS")
  run "${cmd[@]}"
}

repo_has_suite() {
  local suite="$1"
  [ -f "${OFFLINE_ROOT}/iso/dists/${suite}/Release" ]
}

maas_disabled_pockets() {
  local disabled=()
  local pocket=""
  for pocket in updates security backports; do
    if ! repo_has_suite "${SERIES}-${pocket}"; then
      disabled+=("${pocket}")
    fi
  done
  if [ "${#disabled[@]}" -eq 0 ]; then
    printf '%s' ""
    return 0
  fi
  local joined
  joined="$(IFS=,; printf '%s' "${disabled[*]}")"
  printf '%s' "$joined"
}

maas_disabled_components() {
  local release_file="${OFFLINE_ROOT}/iso/dists/${SERIES}/Release"
  local available=""
  local disabled=()
  local component=""

  if [ -f "$release_file" ]; then
    available="$(awk '/^Components:/{for (i=2; i<=NF; i++) print $i}' "$release_file" | tr '\n' ' ')"
  fi

  for component in restricted universe multiverse; do
    if [[ " ${available} " != *" ${component} "* ]]; then
      disabled+=("${component}")
    fi
  done

  if [ "${#disabled[@]}" -eq 0 ]; then
    printf '%s' ""
    return 0
  fi
  local joined
  joined="$(IFS=,; printf '%s' "${disabled[*]}")"
  printf '%s' "$joined"
}

configure_repositories() {
  local disabled_pockets
  local disabled_components
  disabled_pockets="$(maas_disabled_pockets)"
  disabled_components="$(maas_disabled_components)"

  run maas "$PROFILE" boot-source update 1 \
    "url=http://${SERVER_IP}:${HTTP_PORT}/mirror/ephemeral-v3/stable/" \
    keyring_filename=/usr/share/keyrings/ubuntu-cloudimage-keyring.gpg

  configure_boot_source_selection

  run maas "$PROFILE" package-repository update 1 \
    "url=http://${SERVER_IP}:${HTTP_PORT}/iso" \
    "distributions=${SERIES}" \
    "disabled_pockets=${disabled_pockets}" \
    "disabled_components=${disabled_components}" \
    "disable_sources=true"

  upsert_package_repository \
    lldpd_archive \
    "http://${SERVER_IP}:${HTTP_PORT}/tools/lldpd-mini-repo" \
    "$SERIES" \
    main \
    amd64
}

configure_boot_source_selection() {
  if [ "$DRY_RUN" -eq 1 ]; then
    log "would set boot-source selection to os=ubuntu release=${SERIES} arches=${ARCH}"
    return 0
  fi

  local selection_id
  selection_id="$(
    maas "$PROFILE" boot-source-selections read 1 \
      | python3 -c 'import sys,json; data=json.load(sys.stdin); print(data[0]["id"] if data else "")'
  )"

  if [ -n "$selection_id" ]; then
    maas "$PROFILE" boot-source-selection update 1 "$selection_id" \
      os=ubuntu \
      "release=${SERIES}" \
      "arches=${ARCH}" \
      "subarches=*" \
      "labels=*"
  else
    maas "$PROFILE" boot-source-selections create 1 \
      os=ubuntu \
      "release=${SERIES}" \
      "arches=${ARCH}" \
      "subarches=*" \
      "labels=*"
  fi
}

upsert_package_repository() {
  local name="$1"
  local url="$2"
  local distributions="$3"
  local components="$4"
  local arches="$5"

  if [ "$DRY_RUN" -eq 1 ]; then
    log "would upsert package repository ${name}: ${url}"
    return 0
  fi

  local repo_id
  repo_id="$(maas "$PROFILE" package-repositories read | jq -r --arg name "$name" '.[] | select(.name == $name) | .id' | head -n 1)"
  if [ -n "$repo_id" ] && [ "$repo_id" != "null" ]; then
    maas "$PROFILE" package-repository update "$repo_id" \
      "url=${url}" \
      "distributions=${distributions}" \
      "components=${components}" \
      "arches=${arches}" \
      enabled=true
  else
    maas "$PROFILE" package-repositories create \
      "name=${name}" \
      "url=${url}" \
      "distributions=${distributions}" \
      "components=${components}" \
      "arches=${arches}" \
      enabled=true
  fi
}

import_boot_resources() {
  run maas "$PROFILE" boot-resources import
  run sudo systemctl restart maas-rackd maas-regiond
}

install_curtin_template() {
  if [ -z "$POLICY_CONFIG" ]; then
    POLICY_CONFIG="$SCRIPT_DIR/cloud-init/deploy-policy.yaml"
  fi
  local cmd=(sudo python3 "$SCRIPT_DIR/scripts/maas_install_curtin_login_template.py" --config "$POLICY_CONFIG")
  if [ -n "$MACHINES_CSV" ]; then
    cmd+=(--csv "$MACHINES_CSV")
  else
    cmd+=(--policy default --series "$SERIES")
  fi
  run "${cmd[@]}"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --server-ip) SERVER_IP="${2:-}"; shift 2 ;;
    --maas-url) MAAS_URL="${2:-}"; shift 2 ;;
    --offline-root) OFFLINE_ROOT="${2:-}"; shift 2 ;;
    --http-port) HTTP_PORT="${2:-}"; shift 2 ;;
    --profile) PROFILE="${2:-}"; shift 2 ;;
    --admin-user) ADMIN_USER="${2:-}"; shift 2 ;;
    --admin-password) ADMIN_PASSWORD="${2:-}"; shift 2 ;;
    --admin-email) ADMIN_EMAIL="${2:-}"; shift 2 ;;
    --db-uri) DB_URI="${2:-}"; shift 2 ;;
    --db-password) DB_PASSWORD="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --skip-http) SKIP_HTTP=1; shift ;;
    --skip-apt) SKIP_APT=1; shift ;;
    --skip-install) SKIP_INSTALL=1; shift ;;
    --skip-init) SKIP_INIT=1; shift ;;
    --skip-admin) SKIP_ADMIN=1; shift ;;
    --skip-login) SKIP_LOGIN=1; shift ;;
    --skip-repos) SKIP_REPOS=1; shift ;;
    --skip-import) SKIP_IMPORT=1; shift ;;
    --skip-resource-check) SKIP_RESOURCE_CHECK=1; shift ;;
    --maas-control-repo) MAAS_CONTROL_REPO="${2:-}"; shift 2 ;;
    --install-curtin-template) INSTALL_CURTIN_TEMPLATE=1; shift ;;
    --policy-config) POLICY_CONFIG="${2:-}"; shift 2 ;;
    --machines-csv) MACHINES_CSV="${2:-}"; shift 2 ;;
    --maas-dhcp-interface) MAAS_DHCP_INTERFACE="${2:-}"; shift 2 ;;
    --maas-dhcp-start) MAAS_DHCP_START_IP="${2:-}"; shift 2 ;;
    --maas-dhcp-end) MAAS_DHCP_END_IP="${2:-}"; shift 2 ;;
    --maas-dhcp-gateway) MAAS_DHCP_GATEWAY="${2:-}"; shift 2 ;;
    --maas-dhcp-dns) MAAS_DHCP_DNS="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage; exit 2 ;;
  esac
done

detect_server_ip
validate_maas_dhcp_options
if [ -z "$MAAS_URL" ]; then
  MAAS_URL="http://${SERVER_IP}:5240/MAAS"
fi

log "server_ip=${SERVER_IP}"
log "maas_url=${MAAS_URL}"
log "offline_root=${OFFLINE_ROOT}"
log "series=${SERIES}"
if [ -z "$MAAS_CONTROL_REPO" ]; then
  MAAS_CONTROL_REPO="${OFFLINE_ROOT}/tools/maas-control-repo"
fi
MAAS_CONTROL_REPO="${MAAS_CONTROL_REPO/#\$OFFLINE_ROOT/$OFFLINE_ROOT}"

if [ "$SKIP_HTTP" -eq 0 ]; then
  run "$SCRIPT_DIR/maas-offline-oneclick.sh"
fi

if [ "$SKIP_RESOURCE_CHECK" -eq 0 ]; then
  resource_check
fi

if [ "$SKIP_APT" -eq 0 ]; then
  write_apt_sources
fi

if [ "$SKIP_INSTALL" -eq 0 ]; then
  install_packages
fi

if [ "$SKIP_INIT" -eq 0 ]; then
  prepare_postgresql
  upgrade_maas_database
fi

if [ "$SKIP_INIT" -eq 0 ]; then
  init_maas
fi

if [ "$SKIP_ADMIN" -eq 0 ]; then
  create_admin
fi

if [ "$SKIP_LOGIN" -eq 0 ]; then
  start_maas_services
  login_profile
  ensure_maas_dhcp
fi

if [ "$SKIP_REPOS" -eq 0 ]; then
  configure_repositories
fi

if [ "$SKIP_IMPORT" -eq 0 ]; then
  import_boot_resources
fi

if [ "$INSTALL_CURTIN_TEMPLATE" -eq 1 ]; then
  install_curtin_template
fi

cat <<EOF
maas_control_plane_ready=true
server_ip=${SERVER_IP}
maas_url=${MAAS_URL}
offline_http=http://${SERVER_IP}:${HTTP_PORT}
boot_source=http://${SERVER_IP}:${HTTP_PORT}/mirror/ephemeral-v3/stable/
main_archive=http://${SERVER_IP}:${HTTP_PORT}/iso
lldpd_repo=http://${SERVER_IP}:${HTTP_PORT}/tools/lldpd-mini-repo
EOF
