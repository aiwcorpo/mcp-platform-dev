#!/usr/bin/env bash
# `docker compose up --wait` does not wait for one-shot jobs, and `compose wait` fails once the job has
# already exited; poll its state instead. A finished job container is removed afterwards, so that `compose start`
# and Start in Docker Desktop find no exited job to run again (see the note at the top of docker-compose.yml).
set -euo pipefail
compose="$(dirname "$0")/compose.sh"
jobs=(openbao-init server-state-init registry-data-init)
for _ in $(seq 120); do
  state="$("$compose" ps -a --format '{{.State}} {{.ExitCode}}' openbao-init)"
  case "$state" in
    "exited 0")
      for job in "${jobs[@]}"; do
        id="$("$compose" --profile full --profile m2 --profile ci ps -a -q "$job")"
        [ -z "$id" ] || [ "$(docker inspect --format '{{.State.Status}} {{.State.ExitCode}}' "$id")" != "exited 0" ] || docker rm "$id" >/dev/null
      done
      exit 0 ;;
    exited*) echo "openbao-init failed ($state):" >&2; "$compose" logs --tail 20 openbao-init >&2; exit 1 ;;
  esac
  sleep 1
done
echo "openbao-init did not finish" >&2
exit 1
