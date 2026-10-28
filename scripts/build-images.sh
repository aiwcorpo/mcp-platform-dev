#!/usr/bin/env bash
# Builds the three component images of profile "full" from git refs of the sibling repositories without
# touching their working trees (git archive into a temporary directory, jars via ./mvnw, docker build).
# Usage: scripts/build-images.sh [gateway-ref broker-ref jira-ref]   (defaults: the refs below)
# ONLY=gateway,broker,jira builds a subset (default: all three).
# Needs a JDK 25 in JAVA_HOME. Prints the image tags to put into .env.
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
gw_ref="${1:-d4fbf98}"
broker_ref="${2:-9a5d292}"
jira_ref="${3:-HEAD}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

"${JAVA_HOME:?set JAVA_HOME to a JDK 25}/bin/java" -version 2>&1 | grep -q 'version "25' || { echo "JAVA_HOME is not a JDK 25" >&2; exit 1; }

build() { # repo ref image-prefix
  local repo="$1" ref="$2" image="$3" short
  short="$(git -C "$root/$repo" rev-parse --short "$ref")"
  mkdir -p "$work/$repo"
  git -C "$root/$repo" archive "$ref" | tar -x -C "$work/$repo"
  (cd "$work/$repo" && ./mvnw -B -q package -DskipTests)
  docker build -q -t "$image:m1-$short" "$work/$repo" >/dev/null
  echo "$image:m1-$short"
}

want() { [[ -z "${ONLY:-}" || ",$ONLY," == *",$1,"* ]]; }
! want gateway || echo "MCP_GATEWAY_IMAGE=$(build mcp-gateway "$gw_ref" mcp-gateway)"
! want broker || echo "MCP_CREDENTIAL_BROKER_IMAGE=$(build mcp-credential-broker "$broker_ref" mcp-credential-broker)"
! want jira || echo "MCP_JIRA_IMAGE=$(build mcp-server-jira "$jira_ref" mcp-server-jira)"
