#!/bin/bash

set -euo pipefail

PROFILE="${PROFILE:-admin}"
OSYSTEM="${OSYSTEM:-ubuntu}"
SERIES="${SERIES:-jammy}"
SYSID="${1:?usage: $0 system_id [policy-name|user-data.yaml]}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
POLICY_SCRIPT="${SCRIPT_DIR}/maas_policy_deploy.py"
POLICY_CONFIG="${DEPLOY_CONFIG:-${SCRIPT_DIR}/../cloud-init/deploy-policy.yaml}"
DEFAULT_USER_DATA="${SCRIPT_DIR}/../cloud-init/default-user-data.yaml"
SECOND_ARG="${2:-}"
DEPLOY_CSV="${DEPLOY_CSV:-}"

# Backward compatible mode: if a file path is passed, deploy the raw cloud-init file directly.
if [ -n "$SECOND_ARG" ] && [ -f "$SECOND_ARG" ]; then
  USER_DATA_B64="$(base64 -w0 "$SECOND_ARG")"
  maas "$PROFILE" machine deploy "$SYSID" osystem="$OSYSTEM" distro_series="$SERIES" user_data="$USER_DATA_B64"
  exit 0
fi

# Preferred mode: use the policy YAML and auto-match by machine tag.
if [ -f "$POLICY_SCRIPT" ] && [ -f "$POLICY_CONFIG" ]; then
  ARGS=(--profile "$PROFILE" --osystem "$OSYSTEM" --series "$SERIES" --config "$POLICY_CONFIG")
  if [ -n "$DEPLOY_CSV" ]; then
    ARGS+=(--csv "$DEPLOY_CSV")
  fi
  if [ -n "${DEPLOY_POLICY:-}" ]; then
    ARGS+=(--policy "$DEPLOY_POLICY")
  elif [ -n "$SECOND_ARG" ]; then
    ARGS+=(--policy "$SECOND_ARG")
  fi
  exec python3 "$POLICY_SCRIPT" "${ARGS[@]}" "$SYSID"
fi

# Last fallback: keep the old default-user-data.yaml behavior if the policy files are absent.
if [ -f "$DEFAULT_USER_DATA" ]; then
  USER_DATA_FILE="$(mktemp)"
  trap 'rm -f "$USER_DATA_FILE"' EXIT
  HOSTNAME="$(
    maas "$PROFILE" machine read "$SYSID" | python3 -c 'import sys,json; o=json.load(sys.stdin); print(o.get("hostname","machine"))'
  )"
  sed "s/__HOSTNAME__/${HOSTNAME}/g" "$DEFAULT_USER_DATA" > "$USER_DATA_FILE"
  USER_DATA_B64="$(base64 -w0 "$USER_DATA_FILE")"
  maas "$PROFILE" machine deploy "$SYSID" osystem="$OSYSTEM" distro_series="$SERIES" user_data="$USER_DATA_B64"
else
  maas "$PROFILE" machine deploy "$SYSID" osystem="$OSYSTEM" distro_series="$SERIES"
fi
