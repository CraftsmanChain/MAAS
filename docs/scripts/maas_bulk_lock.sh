#!/bin/bash

set -euo pipefail

PROFILE="${PROFILE:-admin}"
TAG="${1:?usage: $0 tag-name}"

maas "$PROFILE" tag machines "$TAG" \
  | python3 -c 'import sys,json; a=json.load(sys.stdin); print("\n".join([m["system_id"] for m in a]))' \
  | while read -r sysid; do
      [ -n "${sysid:-}" ] || continue
      maas "$PROFILE" machine lock "$sysid" comment="bulk lock tag=${TAG}" >/dev/null
    done

