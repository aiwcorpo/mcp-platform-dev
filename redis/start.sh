#!/bin/sh
# Writes the ACL file from environment variables (passwords never appear in argv) and starts Redis.
# Key prefixes and channels per component are a ws-01 proposal, see docs/integracja.md ("Redis").
set -eu

data_cmds="-@all +@read +@write +@string +@hash +@list +@set +@sortedset +@stream +@keyspace +@scripting +@transaction +@connection -@dangerous"

acl=/tmp/users.acl
umask 077
{
  echo "user default off resetkeys resetchannels -@all"
  echo "user admin on >$REDIS_ADMIN_PASSWORD ~* &* +@all"
  echo "user gateway on >$REDIS_GATEWAY_PASSWORD resetkeys ~gw:* resetchannels &credential.invalidated $data_cmds +subscribe +unsubscribe"
  echo "user broker on >$REDIS_BROKER_PASSWORD resetkeys ~broker:* ~jti:mcp-credential-broker:* resetchannels &credential.invalidated $data_cmds +publish"
  for server in jira confluence gitlab servicedesk opensearch; do
    var="REDIS_MCP_$(echo "$server" | tr a-z A-Z)_PASSWORD"
    eval "pw=\${$var}"
    echo "user mcp-$server on >$pw resetkeys ~jti:mcp-$server:* resetchannels $data_cmds"
  done
} > "$acl"

exec redis-server --aclfile "$acl" --appendonly no
