#!/usr/bin/env bash
# Copies a file into the package server's directory (ARTIFACTS_DIR/packages, default ./artifacts/packages) and prints its
# SHA-256 next to the URL it is served under. The server is read-only, so this is the only way in.
#   scripts/publish-package.sh files|maven <source file> <path under that directory>
#   scripts/publish-package.sh files ../mcp-contracts/release/aiwcorpo-mcp-contracts-5.1.0.tgz contracts/5.1.0/aiwcorpo-mcp-contracts-5.1.0.tgz
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
[[ $# -eq 3 && ( "$1" == files || "$1" == maven ) ]] || { sed -n '2,6p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }
kind="$1"; source_file="$2"; target="$3"
[[ -f "$source_file" ]] || { echo "publish-package: $source_file is not a file" >&2; exit 1; }
case "$target" in /*|*..*|"") echo "publish-package: the path must be relative and without .." >&2; exit 1 ;; esac
port="$(grep -s '^PACKAGES_PORT=' "$root/m5.env" | cut -d= -f2-)"
dir="${ARTIFACTS_DIR:-$root/artifacts}/packages/$kind/$target"
mkdir -p "$(dirname "$dir")"
cp "$source_file" "$dir"
echo "$(shasum -a 256 "$dir" | cut -d' ' -f1)  http://localhost:${port:-18756}/$kind/$target"
