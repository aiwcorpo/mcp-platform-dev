#!/bin/sh
# Writes the ACL file from environment variables (passwords never appear in argv) and starts Redis.
# Key prefixes and channels per component are a ws-01 proposal, see docs/integracja.md ("Redis").
set -eu

# Explicit command list instead of categories: key patterns (~prefix) do not restrict SCAN, KEYS, RANDOMKEY
# or scripts, which would reveal other components' key names. A component needing more commands asks ws-01.
data_cmds="-@all +get +set +setnx +getdel +getex +mget +del +unlink +exists +expire +pexpire +ttl +pttl \
+incr +incrby +decr +hset +hget +hdel +hgetall +hexists +hincrby +multi +exec +discard \
+ping +auth +hello +client|setname +client|setinfo +reset +quit"

# Read before the block below: a failing $(cat ...) inside echo would not stop the script and leave a user without commands.
gateway_rules="$(cat /gateway.rules)"
broker_rules="$(cat /broker.rules)"
portal_bff_rules="$(cat /portal-bff.rules)"

acl=/tmp/users.acl
umask 077
{
  echo "user default off resetkeys resetchannels -@all"
  echo "user admin on >$REDIS_ADMIN_PASSWORD ~* &* +@all"
  # Confirmations (C11: pending calls, traces per sub, counters) need sets, sorted sets, WATCH and scripts for the
  # atomic steps the contract requires; scripts are checked against ~gw:* key by key, and without +script and
  # +function nothing can be loaded or flushed. Rules shared with scripts/redis-sync-acl.sh, mounted at /gateway.rules.
  echo "user gateway on >$REDIS_GATEWAY_PASSWORD $gateway_rules"
  # EVAL/EVALSHA for the atomic lock release; Redis checks the keys a script touches against ~broker:*, and
  # without +script and +function nothing can be loaded or flushed.
  echo "user broker on >$REDIS_BROKER_PASSWORD $broker_rules"
  # Spring Session of the portal BFF (ws-07): rules shared with scripts/redis-sync-acl.sh, mounted at /portal-bff.rules.
  echo "user portal-bff on >$REDIS_PORTAL_BFF_PASSWORD $portal_bff_rules"
  for server in jira confluence gitlab servicedesk opensearch; do
    var="REDIS_MCP_$(echo "$server" | tr a-z A-Z)_PASSWORD"
    eval "pw=\${$var}"
    echo "user mcp-$server on >$pw resetkeys ~jti:mcp-$server:* resetchannels $data_cmds"
  done
} > "$acl"

exec redis-server --aclfile "$acl" --dir /data --appendonly yes --appendfsync everysec --maxmemory-policy noeviction
