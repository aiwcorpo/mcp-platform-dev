#!/usr/bin/env bash
# Read-only: which image layers are on and whether the running containers match what Compose would create.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=m6-layers.sh
. "$root/scripts/m6-layers.sh"
top=none
for layer in M4_ACTIVE M5_ACTIVE M6_ACTIVE M6B_ACTIVE M6C_ACTIVE; do
  value="$(layer_flag "$layer")"
  echo "$layer=${value:-unset}"
  [ "$value" != 1 ] || top="$layer"
done
echo "active (highest) layer: $top"
if [ -e "$(m6c_marker)" ]; then echo "marker: $(m6c_marker) exists ($(cat "$(m6c_marker)"))"; else echo "marker: $(m6c_marker) absent"; fi
echo "schema version of the gateway database: $(gateway_schema_version)"
"$root/scripts/compose.sh" --profile full --profile m2 config --format json | python3 -I -c '
import json, subprocess, sys
for name, svc in sorted(json.load(sys.stdin)["services"].items()):
    image = svc.get("image", "")
    if not image.startswith("mcp-"):
        continue
    project = svc.get("container_name") or sys.argv[1] + "-" + name + "-1"
    run = subprocess.run(["docker", "inspect", "-f", "{{.Config.Image}}", project], capture_output=True, text=True).stdout.strip()
    print("%-28s want %-40s running %-40s %s" % (name, image, run or "-", "ok" if run == image else "DIFFERENT"))
' "$(grep -s '^COMPOSE_PROJECT_NAME=' "$root/.env" | cut -d= -f2- || echo mcp-dev)"
