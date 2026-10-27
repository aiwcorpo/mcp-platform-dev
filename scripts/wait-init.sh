#!/usr/bin/env bash
# `docker compose up --wait` does not wait for one-shot jobs, and `compose wait` fails once the job has
# already exited; poll its state instead.
set -euo pipefail
compose="$(dirname "$0")/compose.sh"
for _ in $(seq 120); do
  state="$("$compose" ps -a --format '{{.State}} {{.ExitCode}}' openbao-init)"
  case "$state" in
    "exited 0") exit 0 ;;
    exited*) echo "openbao-init failed ($state):" >&2; "$compose" logs --tail 20 openbao-init >&2; exit 1 ;;
  esac
  sleep 1
done
echo "openbao-init did not finish" >&2
exit 1
