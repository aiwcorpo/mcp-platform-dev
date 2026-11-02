#!/usr/bin/env bash
# One entry point of CI for this repository. Runs the same way on a developer machine (make ci), in the CI image (ci/Dockerfile)
# and under any CI system: .gitlab-ci.yml, a Jenkinsfile or a GitHub workflow only call this script.
#
#   ci/run.sh                 all steps
#   ci/run.sh lint mocks      selected steps: lint mocks unit compose style proxy lb openbao
#
# lint     bash -n and (when installed) shellcheck of the scripts, syntax of the Python files
# mocks    unit tests of the system mocks (incl. latency and 429); no Docker needed
# unit     mocks plus the tests of offboarding and registry registration (need the dev CA, so Docker)
# compose  rendered compose configuration of the M3, M4 and M5 layers (scripts/check-compose.py); the Docker CLI without a daemon
#          is enough
# style    typographic characters, tool traces and placeholder content (ci/check-style.py)
# proxy    rules of the reverse proxy in both layers against stand-in upstreams (needs Docker)
# lb       load balancers of gateway and broker against stand-in instances (needs Docker)
# openbao  init.sh and the persistence of the dev OpenBao on throw-away instances: restart, Stop/Start, unseal, nothing overwritten (needs Docker, about 3 min)
# Steps that need Docker are skipped with a message when no daemon answers, unless CI_REQUIRE_DOCKER=1 (then they fail); under
# CI=true that is the default, and a missing shellcheck is an error too.
# The steps use a throw-away secrets file and CA when none exist (a fresh checkout); an existing .secrets and certs/ are used as they are.
# Nothing here starts, restarts or touches the containers of the running stack.
set -uo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root" || exit 1
steps=("$@")
[ "${#steps[@]}" -gt 0 ] || steps=(lint unit compose style proxy lb openbao)
failed=()
skipped=()

# Under a CI system (CI=true) Docker and shellcheck are required: a skipped step must not read as a green pipeline.
[ "${CI:-}" = true ] && : "${CI_REQUIRE_DOCKER:=1}"

have_docker() { docker info >/dev/null 2>&1; }
need_docker() { # step
  if have_docker; then return 0; fi
  if [ "${CI_REQUIRE_DOCKER:-0}" = 1 ]; then
    echo "ci: $1 needs Docker and CI_REQUIRE_DOCKER=1" >&2
    return 2
  fi
  echo "ci: $1 SKIPPED (no Docker daemon)"
  skipped+=("$1")
  return 1
}

prepare_secrets() { ./scripts/init-secrets.sh >/dev/null; }
prepare() { # secrets and certificates (the CA is generated in a container, so this needs a Docker daemon)
  prepare_secrets || return 1
  if [ ! -f certs/ca/ca.crt ] || [ ! -f certs/public-ca/ca.crt ]; then
    have_docker || return 1
    ./scripts/gen-certs.sh >/dev/null && ./scripts/gen-public-cert.sh >/dev/null || return 1
  fi
}

step_lint() {
  local status=0 file
  for file in scripts/*.sh ci/*.sh docker/*.sh; do
    bash -n "$file" || status=1
  done
  if command -v shellcheck >/dev/null 2>&1; then
    shellcheck -S warning scripts/*.sh ci/*.sh || status=1
  elif [ "${CI_REQUIRE_DOCKER:-0}" = 1 ]; then
    echo "ci: shellcheck is required in CI and not installed" >&2
    status=1
  else
    echo "ci: shellcheck not installed, only bash -n"
  fi
  python3 -I -c '
import ast, sys
for name in sys.argv[1:]:
    ast.parse(open(name).read(), name)' scripts/*.py mocks/systems/*.py ci/*.py proxy/test/*.py || status=1
  return $status
}

step_mocks() { (cd mocks/systems && python3 -I -m unittest discover -s tests); }
step_unit() {
  local status=0
  step_mocks || status=1
  if prepare; then
    python3 -I -m unittest discover -s scripts/tests || status=1
  elif [ "${CI_REQUIRE_DOCKER:-0}" = 1 ]; then
    echo "ci: scripts/tests need the dev CA, generated in a container, and CI_REQUIRE_DOCKER=1" >&2
    status=1
  else
    echo "ci: scripts/tests SKIPPED (they need the dev CA, which is generated in a container: no Docker daemon)"
    skipped+=("unit:scripts")
  fi
  return $status
}

step_compose() { prepare_secrets && python3 -I scripts/check-compose.py; }
step_style() { python3 -I ci/check-style.py; }
step_proxy() {
  need_docker proxy || return $(( $? == 1 ? 0 : 1 ))
  prepare || return 1
  PROXY_LAYER="m4" ./scripts/proxy-test.sh && PROXY_LAYER="m5" ./scripts/proxy-test.sh
}
step_lb() {
  need_docker lb || return $(( $? == 1 ? 0 : 1 ))
  ./scripts/lb-test.sh
}

step_openbao() {
  need_docker openbao || return $(( $? == 1 ? 0 : 1 ))
  ./scripts/openbao-init-test.sh >/dev/null && ./scripts/openbao-persist-test.sh
}

# Validate every name before running anything: an unknown or empty name is an error, never a green run.
for step in "${steps[@]}"; do
  case "$step" in lint|mocks|unit|compose|style|proxy|lb|openbao) ;; *) echo "ci: unknown step '$step'" >&2; exit 2 ;; esac
done

# The step functions report failure through their return status (no reliance on set -e, which is off inside "||").
for step in "${steps[@]}"; do
  echo "ci: ---- $step"
  "step_$step" || failed+=("$step")
done
echo "ci: ---- done; skipped: ${skipped[*]:-none}; failed: ${failed[*]:-none}"
[ "${#failed[@]}" -eq 0 ]
