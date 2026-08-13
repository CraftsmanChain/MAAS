#!/bin/bash

set -euo pipefail

TOOLKIT_ROOT="${TOOLKIT_ROOT:-/opt/maas-offline-toolkit}"
CONFIG_DIR="${CONFIG_DIR:-/etc/maas-offline}"
CONSOLE_USER="${CONSOLE_USER:-ubuntu}"
CONSOLE_GROUP="${CONSOLE_GROUP:-ubuntu}"
LAB_CONFIG="${MAAS_LAB_CONFIG:-${TOOLKIT_ROOT}/docs/lab/two-node-physical.local.json}"
MAAS_PROFILE="${MAAS_PROFILE:-admin}"

if [ "$(id -u)" -ne 0 ]; then
  exec sudo -n "$0" "$@"
fi

for path in \
  "$TOOLKIT_ROOT/web-console/server.py" \
  "$TOOLKIT_ROOT/docs/systemd/maas-web-console.service" \
  "$TOOLKIT_ROOT/docs/systemd/maas-web-console.sudoers"; do
  [ -f "$path" ] || { echo "required file not found: $path" >&2; exit 2; }
done

id "$CONSOLE_USER" >/dev/null 2>&1 || { echo "console user not found: $CONSOLE_USER" >&2; exit 2; }
getent group "$CONSOLE_GROUP" >/dev/null 2>&1 || { echo "console group not found: $CONSOLE_GROUP" >&2; exit 2; }

install -d -m 0755 "$CONFIG_DIR"
install -o root -g root -m 0644 \
  "$TOOLKIT_ROOT/docs/systemd/maas-web-console.service" \
  /etc/systemd/system/maas-web-console.service
install -o root -g root -m 0440 \
  "$TOOLKIT_ROOT/docs/systemd/maas-web-console.sudoers" \
  /etc/sudoers.d/maas-web-console
visudo -cf /etc/sudoers.d/maas-web-console >/dev/null

if [ ! -f "$CONFIG_DIR/web-console.env" ]; then
  install -o root -g root -m 0644 \
    "$TOOLKIT_ROOT/docs/systemd/web-console.env.example" \
    "$CONFIG_DIR/web-console.env"
fi

# The web console runs as CONSOLE_USER, while maas-control-plane-oneclick.sh
# creates its CLI profile for the invoking (normally root) user.  MAAS CLI
# profiles are per-user; without this profile imports fail with
# "invalid choice: admin" even though root can run `maas admin ...`.
if [ -f "$CONFIG_DIR/web-console.env" ]; then
  configured_profile="$(awk -F= '$1 == "MAAS_PROFILE" {print $2; exit}' "$CONFIG_DIR/web-console.env" 2>/dev/null || true)"
  [ -z "$configured_profile" ] || MAAS_PROFILE="$configured_profile"
fi

if [ -f "$LAB_CONFIG" ]; then
  readarray -t maas_settings < <(python3 - "$LAB_CONFIG" <<'PY'
import json
import sys
from pathlib import Path

config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
server = config.get("server") or {}
print(str(server.get("maas_url") or ""))
print(str(server.get("admin_user") or "admin"))
PY
)
  MAAS_URL="${maas_settings[0]:-}"
  MAAS_ADMIN_USER="${maas_settings[1]:-admin}"
else
  MAAS_URL=""
  MAAS_ADMIN_USER="admin"
fi

if [ -n "$MAAS_URL" ] && command -v maas >/dev/null 2>&1; then
  api_key="$(maas apikey --username "$MAAS_ADMIN_USER" 2>/dev/null || true)"
  if [ -n "$api_key" ]; then
    # `maas login` overwrites the selected profile non-interactively.
    sudo -u "$CONSOLE_USER" maas login "$MAAS_PROFILE" "$MAAS_URL" "$api_key" >/dev/null
    echo "maas_console_profile_ready=true profile=${MAAS_PROFILE} user=${CONSOLE_USER}"
  else
    echo "warning: could not create MAAS API key for ${MAAS_ADMIN_USER}; console actions requiring MAAS CLI will remain unavailable" >&2
  fi
else
  echo "warning: MAAS URL/config or maas CLI not available; skipped console-user MAAS profile setup" >&2
fi

chown -R "$CONSOLE_USER:$CONSOLE_GROUP" /srv/maas-offline/stage1
find /srv/maas-offline/stage1 -type d -exec chmod 0755 {} +
find /srv/maas-offline/stage1 -type f -exec chmod 0644 {} +

systemctl daemon-reload
systemctl enable --now maas-web-console.service
systemctl restart maas-web-console.service
systemctl is-active --quiet maas-web-console.service

echo "maas_web_console_ready=true"
echo "console_url=http://0.0.0.0:8088"
