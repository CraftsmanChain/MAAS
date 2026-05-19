#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
POLICY_SCRIPT="${SCRIPT_DIR}/maas_policy_deploy.py"
POLICY_CONFIG="${DEPLOY_CONFIG:-${SCRIPT_DIR}/../cloud-init/deploy-policy.yaml}"
PROFILE="${PROFILE:-}"
SERIES="${SERIES:-}"

ARGS=(--config "$POLICY_CONFIG")
[ -n "$PROFILE" ] && ARGS+=(--profile "$PROFILE")
[ -n "$SERIES" ] && ARGS+=(--series "$SERIES")

exec python3 "$POLICY_SCRIPT" "${ARGS[@]}" "$@"
