#!/bin/bash

set -euo pipefail

PROFILE="${PROFILE:-admin}"
SYSID="${1:?usage: $0 system_id [user-data.yaml]}"
SERIES="${SERIES:-jammy}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_USER_DATA="${SCRIPT_DIR}/../cloud-init/default-user-data.yaml"

if [ "${2:-}" != "" ]; then
  USER_DATA_FILE="$2"
elif [ -f "$DEFAULT_USER_DATA" ]; then
  USER_DATA_FILE="$(mktemp)"
  HOSTNAME="$(
    maas "$PROFILE" machine read "$SYSID" | python3 -c 'import sys,json; o=json.load(sys.stdin); print(o.get("hostname","machine"))'
  )"
  sed "s/__HOSTNAME__/${HOSTNAME}/g" "$DEFAULT_USER_DATA" > "$USER_DATA_FILE"
else
  USER_DATA_FILE=""
fi

if [ -n "${USER_DATA_FILE:-}" ]; then
  USER_DATA_B64="$(base64 -w0 "$USER_DATA_FILE")"
  maas "$PROFILE" machine deploy "$SYSID" distro_series="$SERIES" user_data="$USER_DATA_B64"
else
  maas "$PROFILE" machine deploy "$SYSID" distro_series="$SERIES"
fi
