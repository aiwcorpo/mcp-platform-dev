# Sourced by compose.sh and the switch scripts. Facts about the M6 layers that no script may get wrong:
# once migration V10 of the gateway is in the database (marker out/m6c.applied, written by apply-m6c-switch.sh after V10 was checked, outside git),
# an older gateway image must not be created again, whatever .env or the environment say.
m6c_marker() { echo "${root:?}/out/m6c.applied"; }
# Value of a layer flag: the environment first, then .env.
layer_flag() {
  local name="$1" value
  value="${!name:-}"
  [ -n "$value" ] || value="$(grep -s "^$name=" "${root:?}/.env" | cut -d= -f2- || true)"
  echo "$value"
}
# Schema version of the gateway database, empty when PostgreSQL is not running or does not answer (a `make up` from scratch must not be blocked).
gateway_schema_version() {
  local project container
  project="$(grep -s '^COMPOSE_PROJECT_NAME=' "${root:?}/.env" | cut -d= -f2- || true)"
  container="${project:-mcp-dev}-postgres-1"
  [ "$(docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null || true)" = true ] || return 0
  docker exec "$container" psql -U postgres -d gateway -tAc "select max(version::int) from flyway_schema_history where success" 2>/dev/null | tr -d ' \r' || true
}
# True when V10 is known to be applied: the marker, or (only when asked, as it costs a docker call) the schema.
v10_applied() {
  [ -e "$(m6c_marker)" ] && return 0
  [ "${1:-}" = --check-db ] || return 1
  local version
  version="$(gateway_schema_version)"
  [ -n "$version" ] && [ "$version" -ge 10 ] 2>/dev/null
}
# Way-back scripts of earlier layers call this first, with the flags of the layers newer than the one they undo.
refuse_when_newer_layers() { # script name, flags...
  local layer script="$1"
  shift
  for layer in "$@"; do
    if [ "$(layer_flag "$layer")" = 1 ]; then echo "$script: REFUSED, $layer=1 (a newer layer is on; undo it first, if it can be undone)" >&2; exit 1; fi
  done
  if [ -e "$(m6c_marker)" ]; then echo "$script: REFUSED, $(m6c_marker) exists: migration V10 of the gateway is in the database and images older than m6-dd9769b must not be started" >&2; exit 1; fi
}
