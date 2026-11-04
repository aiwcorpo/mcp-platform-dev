# mcp-platform-dev

A local development and test environment for the MCP platform. It is a tool for people who work on or evaluate the
platform on one machine, not a product and not a deployment recipe. License: Apache-2.0.

It runs Keycloak with the realm `corp`, OpenBao, Redis, PostgreSQL, a Jira mock and mocks of other systems, contract
stand-ins for the credential broker and the gateway admin API, an internal CA with mTLS certificates, and optionally the
real gateway, broker, portal and MCP servers from images you build from the sibling repositories. Scripts issue user
tokens, do token exchange and store personal access tokens through the broker API.

All passwords, client secrets and keys are generated on the first run into `.secrets/` and `certs/` (both git-ignored).
They are test material only, **NOT FOR PRODUCTION**.

Documentation of the platform lives in the `mcp-docs` repository. The notes in `docs/` are in English; `STATUS.md` of this
repository is a working log in Polish.

## Requirements

- Linux or macOS. Scripts need bash 3.2 or newer (the macOS default works). On Windows use WSL2 with a Linux
  distribution and Docker integration, and run `make` and all scripts inside it.
- Docker with `docker compose` v2 (tested with Docker 28.3 and Compose 2.39). About 2.1 GB of images after unpacking
  (Keycloak 0.75 GB); the first pull depends on your connection.
- `make`, `bash`, `python3` 3.10 or newer (standard library only), `openssl`, `git`.
- Sibling repository `mcp-contracts` next to this one (`../mcp-contracts`). The contract stand-ins are extracted from
  the tag `MCP_CONTRACTS_REF` with `git archive` into `out/contracts/`; the working copy is not used. Another location:
  `MCP_CONTRACTS_DIR` in `.env`.
- To build component images (`make images`): a JDK 25 with `JAVA_HOME` pointing to it, and node/npm for the web build.
  The build scripts stop with an error when `JAVA_HOME` is unset or is not a JDK 25.

## Start

```bash
make up        # secrets and certificates (once), contracts, profile infra; waits for health checks
make smoke     # smoke tests: tokens, token exchange, Jira mock, mTLS, OpenBao, Redis ACL, PostgreSQL, networks
make users     # logins and generated passwords of the test users
```

The first `make up` on a machine with the images already pulled takes about 20 s.

Frequent commands:

```bash
make claims USER=alice                         # decoded token of the claude-code client (PKCE): aud, azp, groups
make token USER=alice                          # the token alone (for curl -H "Authorization: Bearer ...")
make exchange USER=alice REQUESTER=mcp-gateway # token exchange to aud=mcp-credential-broker
make pat USER=alice SYSTEM=jira                # store the PAT alice-pat through the broker API
make audit RID=<requestId> [LOGS="a.log b.log"]  # audit events of one request (containers and component logs on the host)
make jira-auth                                 # which credentials reached the Jira mock
make images                                    # build gateway, broker and mcp-jira images from the sibling repositories
make layers                                    # read-only: active image layers, schema version, expected vs. running images
make ps                                        # project containers, also the exited ones
make trust-ca                                  # print the commands that trust the CA of the https proxy
make down | make reset | make clean            # stop; reset state; remove generated files
```

`make help` lists every target. Scenario and test targets (`make m1`, `m2`, `m3`, `m4`, `proxy-test`, `mocks-test`,
`lb-test`, `openbao-persist-test`, `e2e-prod-test` and others) are described in `docs/`. Some of them change the state of
the stack (restart a container, remove an authenticator); the description says which. Always run compose through `make`
or `scripts/compose.sh`: the script passes `.env` and `.secrets/dev.env` as `--env-file`, so each service receives only
its own secrets.

`make smoke` is read-only. It checks container health, claims and token exchange on a throw-away Keycloak account
(created and removed; alice, bob, carol and mallory are only read through the admin API, never logged in), the Jira mock,
mTLS of the broker (certificate refusals on a running port), OpenBao (AppRole, Transit key, audit), Redis ACLs,
PostgreSQL, networks, certificates and the audit tool. It leaves a purge marker in Redis (TTL 30 min) and two
`admin_change` audit events. Expected result: 78 `PASS` lines and `all checks passed` (exit 0); otherwise a list of
`FAIL` and exit 1. Checks that write (Redis keys with TTL, an OpenBao KV path, a Jira mock mapping, a wrong password, an
anonymous client registration, POSTs to the mock admin API) run only in `make smoke-write CONFIRM=1`.

## Compose profiles

| Profile | Command | What runs |
| --- | --- | --- |
| (none) | `make up-core` | Keycloak, OpenBao (persistent volume `openbao-data`, `openbao-unseal`, one-shot `openbao-init`), Redis, PostgreSQL, Jira mock |
| `infra` | `make up` | the above plus WireMock stand-ins of the broker and the gateway admin API from `mcp-contracts` |
| `full` | `make up-full` | infrastructure plus `mcp-gateway`, `mcp-credential-broker` and `mcp-jira` from the images in `.env`, overridden by the layer files below when active; the stand-ins are removed |

`up-core` is for work that runs its own component on the host and does not want a stand-in at the same address.

Optional profiles are separate compose files outside the default stack and are not enabled by `make up*`:

| Profile | Command | What it gives |
| --- | --- | --- |
| `bulk` | `make bulk-on CONFIRM=1` / `make bulk-off CONFIRM=1` (without `CONFIRM=1`: checks and a plan only) | a stand-in with 250 tools in place of `mcp-confluence`, for a tool-list size test; changes the stack |
| `adapter` | `scripts/adapter.sh on\|off --confirm` | a read-only Jira demo through the server adapter in place of the native `mcp-jira` |
| `e2e-prod` | `make e2e-prod-up`, `make e2e-prod-test`, `make e2e-prod-down` | a separate project: gateway without the dev profile, broker and a stand-in MCP server; does not touch the main stack |

## Image layers

The images in `.env` are the base state. Later layers override them with `m4.env`, `m5.env`, `m6.env`, `m6b.env` and
`m6c.env` (the first two also with `docker-compose.m4.yml` and `docker-compose.m5.yml`). The flags `M4_ACTIVE`,
`M5_ACTIVE`, `M6_ACTIVE`, `M6B_ACTIVE`, `M6C_ACTIVE` (value 1) are in `.env` and are set by `make m4-switch`,
`make m5-switch` and `scripts/apply-m6*-switch.sh`; an environment variable of the same name wins. `scripts/compose.sh`
adds the files in order and the last wins, so the images of the highest active layer apply. After the gateway database
migration, `apply-m6c-switch.sh` writes the marker `out/m6c.applied` (outside git), and `compose.sh` then refuses `up`,
`create` and `run` without `M6C_ACTIVE=1` (exit code 5). Details of each switch are in `docs/`.

## Trying it with Claude Code

`make up-full` starts the real gateway, broker and `mcp-jira`. The browser login cannot be automated, so this part is
manual. Anonymous dynamic client registration is blocked in the realm (403), so Claude Code uses the registered public
client `claude-code` with the fixed callback port 33418 (it must be free on the host).

```bash
make up-full
make pat USER=alice SYSTEM=jira BROKER_URL=https://localhost:18100   # 200 connected
make users                                                          # alice's password for the login screen
claude mcp add --transport http --client-id claude-code --callback-port 33418 \
  jira http://localhost:18000/s/mcp-jira/mcp
```

With the proxy in front (one public domain) the address is `https://localhost:18745` and the command from
[docs/claude-code.md](docs/claude-code.md) applies, together with trusting the CA (`make trust-ca`). In Claude Code run
`/mcp`, choose `jira`, `Authenticate`, and log in as `alice` in the Keycloak window. A test prompt: `fetch ABC-1`; the
expected result is a call of `jira__get_issue` and the data of ABC-1 from the mock. On the platform side:

```bash
make jira-auth      # only "PAT alice-pat" reached the mock, never an IdP token
RID=$(docker logs mcp-dev-mcp-gateway-1 2>&1 | grep '"event":"tool_call"' | tail -1 | sed -E 's/.*"requestId":"([^"]+)".*/\1/')
make audit RID=$RID # gateway, broker and server events of one requestId, "missing: none"
```

No PAT is stored on the workstation: the Keycloak token has `aud=mcp-gateway` and the gateway fetches the Jira
credential from the broker. To switch the user in Claude Code: `/mcp`, `jira`, `Clear authentication`, log in again.

## Documentation

- [docs/porty.md](docs/porty.md): addresses and ports of the services, from the host and from the compose networks
- [docs/uzytkownicy.md](docs/uzytkownicy.md): users, groups, OIDC clients, token exchange
- [docs/integracja.md](docs/integracja.md): how to connect the gateway, broker, MCP server, portal and the end-to-end tests
- [docs/claude-code.md](docs/claude-code.md): connecting Claude Code to the local gateway
- [docs/m4.md](docs/m4.md), [docs/m5.md](docs/m5.md), [docs/m6.md](docs/m6.md): per-layer setup and switches
- [STATUS.md](STATUS.md): working log (in Polish)

The platform documentation is in `mcp-docs`.

## Layout

```
docker-compose.yml        services and profiles
.env                      ports and component images (no secrets)
keycloak/realm-corp.json  realm corp (imported at start; passwords and secrets as ${VARIABLES})
mocks/                    Jira mock (generated by scripts/gen-jira-mock.py), mocks of other systems, MCP and broker stubs
openbao/                  init.sh (KV v2, Transit, broker AppRole), server.hcl (raft on a volume), unseal.sh, audit.hcl
redis/                    start.sh: users and ACLs per component
postgres/init/            gateway and broker databases
proxy/, lb/, policies/    nginx configuration of the proxy, the balancers and the policy store
scripts/                  init-secrets.sh, gen-certs.sh, fetch-contracts.sh, compose.sh, mcpdev.py, smoke.py, ...
ci/                       ci/run.sh is the single CI entry point (GitHub Actions, GitLab CI and Jenkins call it)
.secrets/, certs/, out/   generated, git-ignored
```
