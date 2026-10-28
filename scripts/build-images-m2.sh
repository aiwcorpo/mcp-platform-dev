#!/usr/bin/env bash
# Builds the images of profile "m2" from git refs of the sibling repositories (git archive into a temporary
# directory; the working trees are not touched): the portal BFF and web (repo mcp-portal, subdirectories bff/ and
# web/) and the MCP servers of ws-04 (confluence, gitlab, servicedesk, opensearch).
# Usage: scripts/build-images-m2.sh [portal-ref] [server-ref]   (defaults: HEAD of each repository)
# ONLY=bff,web,confluence,gitlab,servicedesk,opensearch builds a subset (default: all).
# Needs a JDK 25 in JAVA_HOME and node/npm for the web build. Prints the image tags to put into .env.
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
dev="$root/mcp-platform-dev"
portal_ref="${1:-HEAD}"
server_ref="${2:-HEAD}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

"${JAVA_HOME:?set JAVA_HOME to a JDK 25}/bin/java" -version 2>&1 | grep -q 'version "25' || { echo "JAVA_HOME is not a JDK 25" >&2; exit 1; }

archive() { # repo ref -> prints short sha, extracts into $work/<repo>
  mkdir -p "$work/$1"
  git -C "$root/$1" archive "$2" | tar -x -C "$work/$1"
  git -C "$root/$1" rev-parse --short "$2"
}

want() { [[ -z "${ONLY:-}" || ",$ONLY," == *",$1,"* ]]; }

if want bff || want web; then short="$(archive mcp-portal "$portal_ref")"; fi
if want bff; then
  (cd "$work/mcp-portal/bff" && ./mvnw -B -q package -DskipTests)
  ctx="$work/bff-ctx"
  mkdir -p "$ctx"
  cp "$(ls "$work"/mcp-portal/bff/target/mcp-portal-bff-*.jar | grep -v original | head -1)" "$ctx/app.jar"
  cp "$dev/docker/bff-entrypoint.sh" "$ctx/bff-entrypoint.sh"
  docker build -q -f "$dev/docker/bff.Dockerfile" -t "mcp-portal-bff:m2-$short" "$ctx" >/dev/null
  echo "MCP_PORTAL_BFF_IMAGE=mcp-portal-bff:m2-$short"
fi

if want web; then
  (cd "$work/mcp-portal/web" && npm ci --silent && npm run build --silent)
  ctx="$work/web-ctx"
  mkdir -p "$ctx/dist"
  cp -R "$work/mcp-portal/web/dist/portal" "$ctx/dist/portal"
  cp -R "$dev/portal-web" "$ctx/portal-web"
  docker build -q -f "$dev/docker/web.Dockerfile" -t "mcp-portal-web:m2-$short" "$ctx" >/dev/null
  echo "MCP_PORTAL_WEB_IMAGE=mcp-portal-web:m2-$short"
fi

for system in confluence gitlab servicedesk opensearch; do
  want "$system" || continue
  repo="mcp-server-$system"
  short="$(archive "$repo" "$server_ref")"
  (cd "$work/$repo" && ./mvnw -B -q package -DskipTests)
  docker build -q -t "$repo:m2-$short" "$work/$repo" >/dev/null
  echo "MCP_$(echo "$system" | tr a-z A-Z)_IMAGE=$repo:m2-$short"
done
