#!/usr/bin/env bash
# Test of the database part of the M5 rollback (scripts/m5-lib.sh) on a throw-away PostgreSQL in an isolated network. It never
# touches the stack: the data below is made up. A migration of M5 is simulated (V7 drops virtual_servers, other data changes),
# then the backup is restored and compared.
# Usage: scripts/rollback-test.sh
set -uo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
net=mcp-dev-rollbacktest
name="$net-pg"
image="$(sed -n 's/^    image: \(postgres:[^ ]*\)$/\1/p' "$root/docker-compose.yml" | head -1)"
pass=0
fail=0
tmp="$(mktemp -d)"
trap 'docker rm -f "$name" >/dev/null 2>&1; docker network rm "$net" >/dev/null 2>&1; rm -rf "$tmp"' EXIT

check() { # name expected actual
  if [[ "$2" == "$3" ]]; then pass=$((pass + 1)); else fail=$((fail + 1)); echo "FAIL $1: expected '$2', got '$3'"; fi
}
pg_exec() { docker exec -i "$name" "$@"; }
sql() { # db statement
  pg_exec psql -U "$1" -d "$1" -At -v ON_ERROR_STOP=1 -c "$2"
}
# shellcheck source=m5-lib.sh
. "$root/scripts/m5-lib.sh"

docker network create "$net" >/dev/null || exit 1
docker run -d --name "$name" --network "$net" -e POSTGRES_PASSWORD=throwaway "$image" >/dev/null || exit 1
for _ in $(seq 1 60); do docker exec "$name" pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done
sleep 2
pg_exec psql -U postgres -v ON_ERROR_STOP=1 -q <<'SQL' || exit 1
CREATE ROLE gateway LOGIN; CREATE ROLE broker LOGIN;
CREATE DATABASE gateway OWNER gateway; CREATE DATABASE broker OWNER broker;
SQL
sql gateway "create table flyway_schema_history (installed_rank int primary key, version text, success boolean);
  insert into flyway_schema_history select n, n::text, true from generate_series(1, 6) n;
  create table virtual_servers (slug text primary key, owner_sub text, tools text[]);
  insert into virtual_servers values ('team-a', 'sub-1', '{jira__get_issue,jira__search_issues}'), ('team-b', 'sub-2', '{gitlab__get_project}');
  create table registry_servers (server_id text primary key, source text);
  insert into registry_servers values ('mcp-jira', 'ci'), ('mcp-gitlab', 'ci');" >/dev/null
sql broker "create table connection (sub text, system text, state text);
  insert into connection values ('sub-1', 'jira', 'connected'), ('sub-1', 'gitlab', 'connected'), ('sub-2', 'jira', 'connected');
  create table authenticator (sub text primary key, state text, registration_id text);
  insert into authenticator values ('sub-1', 'active', 'reg-1');" >/dev/null
digest() { sql "$1" "$2" | md5 2>/dev/null || sql "$1" "$2" | md5sum; }
before_links="$(digest gateway 'select * from virtual_servers order by slug')"
before_registry="$(digest gateway 'select * from registry_servers order by server_id')"
before_connections="$(digest broker 'select * from connection order by sub, system')"
before_auth="$(digest broker 'select * from authenticator order by sub')"

backup="$tmp/backup"
mkdir -p "$backup"
m5_backup_databases "$backup" >/dev/null
check "backup: both dumps complete" "1 1" "$(grep -c 'PostgreSQL database dump complete' "$backup/gateway.sql") $(grep -c 'PostgreSQL database dump complete' "$backup/broker.sql")"
check "backup: table dump holds DDL and data" "1 1" "$(grep -c 'CREATE TABLE public.virtual_servers' "$backup/virtual_servers.sql") $(grep -c 'team-b' "$backup/virtual_servers.sql")"
check "backup: flyway versions recorded" "1 2 3 4 5 6" "$(tr '\n' ' ' < "$backup/gateway-flyway-versions.txt" | sed 's/ $//')"

# What M5 does: V7 drops the links and is recorded; other data moves on; a table of M5 appears.
sql gateway "drop table virtual_servers; insert into flyway_schema_history values (7, '7', true);
  update registry_servers set source = 'bootstrap'; create table m5_only (x int);" >/dev/null
sql broker "delete from connection where sub = 'sub-2'; update authenticator set state = 'disabled';" >/dev/null

m5_restore_databases "$backup" >/dev/null
check "restore: the dropped table is back with its rows" "$before_links" "$(digest gateway 'select * from virtual_servers order by slug')"
check "restore: the V7 row is gone from the flyway history" "1 2 3 4 5 6" "$(sql gateway 'select version from flyway_schema_history order by installed_rank' | tr '\n' ' ' | sed 's/ $//')"
check "restore: gateway data back" "$before_registry" "$(digest gateway 'select * from registry_servers order by server_id')"
check "restore: broker connections back" "$before_connections" "$(digest broker 'select * from connection order by sub, system')"
check "restore: authenticator back" "$before_auth" "$(digest broker 'select * from authenticator order by sub')"
check "restore: a table that only M5 created stays (the dump does not know it)" 1 "$(sql gateway "select count(*) from pg_tables where tablename = 'm5_only'")"

# A restore that fails changes nothing (single transaction).
sql gateway "insert into registry_servers values ('mcp-x', 'ci')" >/dev/null
cp "$backup/gateway.sql" "$tmp/broken.sql"
echo 'SELECT * FROM table_that_does_not_exist;' >> "$tmp/broken.sql"
mkdir -p "$tmp/broken"; cp "$tmp/broken.sql" "$tmp/broken/gateway.sql"; cp "$backup/broker.sql" "$tmp/broken/"
m5_restore_databases "$tmp/broken" >/dev/null 2>&1
check "a failing restore reports failure" 1 "$(( $? != 0 ))"
check "a failing restore leaves the database as it was" 3 "$(sql gateway 'select count(*) from registry_servers')"

# A dump that cannot be made is an error, not an empty file.
pg_exec() { docker exec -i "$name" "$@" -x-bogus 2>/dev/null; }
mkdir -p "$tmp/bad"
m5_backup_databases "$tmp/bad" >/dev/null 2>&1
check "backup: a failing pg_dump fails the backup" 1 "$(( $? != 0 ))"

# The plan of the rollback script (dry run: touches nothing, needs no stack).
plan="$("$root/scripts/rollback-m5.sh" --dry-run "$backup" 2>&1)"
check "rollback plan: removes balancers and second instances first" "would run: m5 rm -sf gateway-lb broker-lb mcp-gateway-2 mcp-credential-broker-2" "$(grep 'rm -sf' <<<"$plan" | sed 's/^ *//')"
check "rollback plan: restores databases, drops M5_ACTIVE, recreates the M4 services" "1 1 1 1" \
  "$(grep -c 'restore gateway and broker databases' <<<"$plan") $(grep -c 'remove M5_ACTIVE' <<<"$plan") $(grep -c 'm4 up -d --no-deps --wait mcp-gateway$' <<<"$plan") $(grep -c 'm4 up -d --no-deps --wait proxy' <<<"$plan")"
check "rollback plan: refuses an empty backup" 1 "$(rm -rf "$tmp/empty"; mkdir "$tmp/empty"; "$root/scripts/rollback-m5.sh" --dry-run "$tmp/empty" >/dev/null 2>&1; echo $(( $? != 0 )))"
check "rollback plan: the dry run left .env alone" 0 "$(grep -c '^M5_ACTIVE=' "$root/.env")"

echo "rollback tests: $pass passed, $fail failed"
[[ "$fail" -eq 0 ]]
