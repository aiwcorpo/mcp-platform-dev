# shellcheck shell=bash
# Sourced by compose.sh and the fetch scripts. MCP_IMAGE_SOURCE: "local" (default; m*.env images built from the sibling repositories)
# or "ghcr" (ghcr.env: released images and artifacts). The environment wins over .env.
image_source() {
  local value="${MCP_IMAGE_SOURCE:-}"
  [ -n "$value" ] || value="$(grep -s '^MCP_IMAGE_SOURCE=' "${root:?}/.env" | cut -d= -f2- || true)"
  case "${value:-local}" in
    local|ghcr) echo "${value:-local}" ;;
    *) echo "image-source: MCP_IMAGE_SOURCE must be local or ghcr, not '$value'" >&2; return 1 ;;
  esac
}
# Value of a variable of ghcr.env.
ghcr_value() { grep "^$1=" "${root:?}/ghcr.env" | cut -d= -f2-; }
