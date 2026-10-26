#!/bin/sh
# Databases for the gateway (virtual servers, registry, activity) and the broker (connection metadata,
# never secret values). Runs only on an empty data volume.
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
  -v gw_pw="$GATEWAY_DB_PASSWORD" -v br_pw="$BROKER_DB_PASSWORD" <<'SQL'
CREATE ROLE gateway LOGIN PASSWORD :'gw_pw';
CREATE ROLE broker LOGIN PASSWORD :'br_pw';
CREATE DATABASE gateway OWNER gateway;
CREATE DATABASE broker OWNER broker;
REVOKE ALL ON DATABASE gateway FROM PUBLIC;
REVOKE ALL ON DATABASE broker FROM PUBLIC;
SQL
