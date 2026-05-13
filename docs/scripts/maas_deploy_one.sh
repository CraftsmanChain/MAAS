#!/bin/bash

set -euo pipefail

PROFILE="${PROFILE:-admin}"
SYSID="${1:?usage: $0 system_id [user-data.yaml]}"
SERIES="${SERIES:-jammy}"

if [ "${2:-}" != "" ]; then
  USER_DATA_B64="$(base64 -w0 "$2")"
  maas "$PROFILE" machine deploy "$SYSID" distro_series="$SERIES" user_data="$USER_DATA_B64"
else
  maas "$PROFILE" machine deploy "$SYSID" distro_series="$SERIES"
fi

