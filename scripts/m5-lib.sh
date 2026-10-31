#!/usr/bin/env bash
# Shared by apply-m5-switch.sh, rollback-m5.sh and rollback-test.sh (sourced). The caller defines
#   pg_exec <command> [args...]    runs a command (psql, pg_dump) in the PostgreSQL container, stdin passed through
# Dumps are complete (schema and data) and restore with --clean, so a migration that dropped a table (V7 of the M5 gateway drops
# virtual_servers) is undone together with its row in flyway_schema_history.

m5_backup_databases() { # dir
  local dir="$1" db exists
  for db in gateway broker; do
    pg_exec pg_dump -U "$db" -d "$db" --clean --if-exists --no-owner --no-privileges > "$dir/$db.sql" || return 1
    tail -n 12 "$dir/$db.sql" | grep -q 'PostgreSQL database dump complete' || { echo "m5-lib: the dump of $db is incomplete" >&2; return 1; }
  done
  # The query must succeed; "no table" is the answer f, not an error.
  exists="$(pg_exec psql -U gateway -d gateway -At -v ON_ERROR_STOP=1 -c "select to_regclass('public.virtual_servers') is not null")" || return 1
  case "$exists" in
    t) pg_exec pg_dump -U gateway -d gateway --table=virtual_servers --no-owner --no-privileges > "$dir/virtual_servers.sql" || return 1
       echo "m5-lib: virtual_servers saved ($(pg_exec psql -U gateway -d gateway -At -c 'select count(*) from virtual_servers') rows)" ;;
    f) echo "m5-lib: no table virtual_servers" ;;
    *) echo "m5-lib: unexpected answer '$exists' about virtual_servers" >&2; return 1 ;;
  esac
  pg_exec psql -U gateway -d gateway -At -v ON_ERROR_STOP=1 -c 'select version from flyway_schema_history order by installed_rank' > "$dir/gateway-flyway-versions.txt" || return 1
}

m5_restore_databases() { # dir
  local dir="$1" db
  for db in gateway broker; do
    [ -s "$dir/$db.sql" ] || { echo "m5-lib: $dir/$db.sql is missing" >&2; return 1; }
    pg_exec psql -U "$db" -d "$db" -q -1 -v ON_ERROR_STOP=1 < "$dir/$db.sql" >/dev/null || return 1
    echo "m5-lib: $db restored from $dir/$db.sql"
  done
}
