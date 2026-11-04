# Attaching components

There are two working modes:

1. **Component on the host** (IDE, `./mvnw spring-boot:run`) against `make up` (with the broker and gateway-admin stubs) or
   `make up-core` (without stubs, when your own broker or gateway runs on the host). Addresses are in the "from the host" column of
   [porty.md](porty.md).
2. **Component in compose** (`make up-full`): the image comes from `.env` (`MCP_GATEWAY_IMAGE`, `MCP_CREDENTIAL_BROKER_IMAGE`,
   `MCP_JIRA_IMAGE`), addresses from the compose network. `make up-full` removes the stubs (they have the same DNS aliases as the
   real broker and gateway), and `make up` removes the components of the `full` profile. Services get the `MCP_*` variables
   described below; a component maps them to its own configuration (for example `application-dev.yml` with `${MCP_OIDC_ISSUER}`).
   The variable names are a proposal of the environment owners; to change them, edit `docker-compose.yml` in this repo.

## TLS names of the broker and gateway

Decision: the name in the certificate of the broker and of the internal gateway endpoint (JWKS, `/admin/v1`) is not
a contract convention but a configuration parameter of each component. The environment sets them in `.env`:

| Variable | Default | Where used |
| --- | --- | --- |
| `MCP_BROKER_TLS_NAME` | `mcp-credential-broker.internal` | SAN of the `certs/mcp-credential-broker` certificate, DNS alias of the broker and of the broker stub, `MCP_BROKER_TLS_NAME` of the gateway, verification in `make pat` |
| `MCP_GATEWAY_INTERNAL_TLS_NAME` | `mcp-gateway.internal` | SAN of the `certs/mcp-gateway` certificate, DNS alias of the gateway and of the gateway-admin stub, `MCP_GATEWAY_TLS_NAME` of the broker and of `mcp-jira` |

A client must check this name regardless of the address it connects to (for example `https://localhost:18100` from the
host). `make pat` does so: it connects to `BROKER_URL` and verifies the certificate against `MCP_BROKER_TLS_NAME`;
a certificate with a different name is rejected (checked by `make smoke`). To change a name: edit `.env`, then
`make clean && make up` (new certificates).

## Contracts version

The broker and gateway-admin stubs and the audit vectors come from `mcp-contracts` at the version `MCP_CONTRACTS_REF` (`.env`, currently `v1.1.0`).
`make up` (target `contracts`) runs `git archive <ref>` from `../mcp-contracts` into `out/contracts/` (ignored
by git) and recreates the stubs. The `mcp-contracts` working copy is not mounted, so work in progress in that
repo does not affect the environment.

## Gateway

| Need | Source |
| --- | --- |
| identity token validation | JWKS `http://keycloak:8080/realms/corp/protocol/openid-connect/certs` (from the host `http://localhost:18080/...`), `iss` `https://localhost:18746/realms/corp`, `aud` `mcp-gateway` |
| RFC 9728 `authorization_servers` | `https://localhost:18746/realms/corp` (the address Claude Code sees on the host) |
| `subjectToken` for `resolve` (broker contract) | client `mcp-gateway`, secret `KC_CLIENT_MCP_GATEWAY_SECRET`, `audience=mcp-credential-broker` + `scope=mcp-credential-broker-audience`; the gateway gets no other audience |
| mTLS client to the broker and MCP servers | `certs/mcp-gateway/keystore.p12` (CN `mcp-gateway`, EKU serverAuth+clientAuth), trust `certs/ca/truststore.p12` |
| broker name for verification | `MCP_BROKER_TLS_NAME` |
| MCP server SAN check | servers have the SAN `<serverId>.internal`, for example `mcp-jira.internal` |
| own internal endpoint (JWKS of the internal JWT) | `https://${MCP_GATEWAY_INTERNAL_TLS_NAME}:8443/.well-known/jwks.json` with the `certs/mcp-gateway` certificate; the gateway generates its signing keys itself |
| broker | `make up`: stub `https://localhost:18443` / `https://mcp-credential-broker.internal:8443`; `make up-full`: the real broker under the same alias |
| Redis | user `gateway`, password `REDIS_GATEWAY_PASSWORD`, keys `gw:*`, subscription `credential.invalidated` |
| PostgreSQL | database `gateway`, user `gateway`, password `GATEWAY_DB_PASSWORD` |
| audit | one JSON line per event on the container stdout (this is what `make audit` reads) |

The broker stub does not know users: `resolve` always returns `test-only-jira-pat-0000000000000000`, and the Jira
mock presents that PAT as `jkowalski`. Only the real broker returns `alice-pat` stored by `make pat`.

## Credential broker

| Need | Source |
| --- | --- |
| mTLS server | `certs/mcp-credential-broker/keystore.p12` (SAN `${MCP_BROKER_TLS_NAME}`, `localhost`; EKU serverAuth only), `clientAuth` required, trust `certs/ca/truststore.p12` |
| caller identification | CN of the client certificate: `mcp-gateway`, `mcp-portal-bff`, `mcp-offboarding` (the last two have EKU clientAuth only); foreign CA: `certs/untrusted/mcp-gateway` |
| BFF token | `aud=mcp-credential-broker`, `azp=mcp-portal-bff`, Keycloak `iss` and JWKS as above |
| gateway token | internal JWT (gateway JWKS, TLS name `MCP_GATEWAY_INTERNAL_TLS_NAME`), `aud=mcp-credential-broker` |
| exchange of `subjectToken` for a `token-exchange` system | client `mcp-credential-broker`, secret `KC_CLIENT_MCP_CREDENTIAL_BROKER_SECRET`, `audience=opensearch` + `scope=opensearch-audience` |
| OpenBao | `http://openbao:8200` / `http://localhost:18200`; AppRole `role_id=BAO_BROKER_ROLE_ID`, `secret_id=BAO_BROKER_SECRET_ID` (valid 30 days; `openbao-init`, that is `make up` and `make openbao-init`, renews it when less than half of the TTL is left, `make openbao-renew` on demand; OpenBao is persistent, see docs/m5.md); policy: KV v2 `secret/data/mcp/connections/*`, Transit `transit/{encrypt,decrypt}/mcp-credential-broker`; the OpenBao audit goes to the stdout of the `openbao` container |
| Jira whoami | `MCP_JIRA_URL=http://jira.internal:8080` (network `core`) / `http://localhost:18081` from the host |
| `connectUrl` in `409 credential_required` | `MCP_PORTAL_URL=http://localhost:18090` |
| http to OpenBao and the Jira mock | `MCP_ALLOW_PLAIN_HTTP=true` only in compose dev; outside dev the broker requires https |
| PostgreSQL | database `broker`, user `broker`, password `BROKER_DB_PASSWORD` (no access to the `gateway` database) |
| Redis | user `broker`, password `REDIS_BROKER_PASSWORD`, keys `broker:*` and `jti:mcp-credential-broker:*`, publishing on `credential.invalidated` |

Acceptance test for storing a PAT on the real broker (port 18100; the broker alone from the image
`mcp-credential-broker:dev`: `./scripts/compose.sh --profile full up -d --no-deps mcp-credential-broker`,
after the test `./scripts/compose.sh --profile full rm -sf mcp-credential-broker`, because it has the same alias as the broker stub).
Checked on 2026-10-27 with broker commit c59dc82:

```bash
make pat USER=alice SYSTEM=jira BROKER_URL=https://localhost:18100              # 200, backendUser alice
make pat USER=alice SYSTEM=jira SECRET=bob-pat BROKER_URL=https://localhost:18100   # 422 identity_mismatch
```

The script does the same as the BFF: logs in `alice` in the `mcp-portal-bff` client (authorization code + PKCE),
runs a token exchange for `aud=mcp-credential-broker`, sends `PUT /v1/connections/{system}/secret` over mTLS with the
`mcp-portal-bff` certificate, and verifies the name `MCP_BROKER_TLS_NAME`. The PAT reaches the script through an environment variable
(`MCP_PAT`, set by `make` from `SECRET`), not through process arguments, and it is never printed.

## MCP server, for example `mcp-jira`

| Need | Source |
| --- | --- |
| mTLS server | `certs/mcp-jira/keystore.p12` (CN `mcp-jira`, SAN `mcp-jira.internal`, EKU serverAuth only); the only client is `mcp-gateway` |
| internal JWT validation | gateway JWKS `https://${MCP_GATEWAY_INTERNAL_TLS_NAME}:8443/.well-known/jwks.json` (TLS name in `MCP_GATEWAY_TLS_NAME`), `iss` `https://mcp-gateway.internal`; for tests without a gateway: keys and vectors from `mcp-contracts/test-vectors` |
| `jti` replay protection | Redis, user `mcp-jira`, password `REDIS_MCP_JIRA_PASSWORD`, keys `jti:mcp-jira:*` only |
| Jira | `MCP_JIRA_URL=http://jira.internal:8080` (in the template `MCP_<SYSTEM>_URL`, in the example `MCP_EXAMPLE_URL`), credential from `X-MCP-Credential` (`Bearer <PAT>`) |
| `connectUrl` in `credential_required` | `MCP_CONNECT_URL=http://localhost:18090/connections?system={system}`, the same shape as the broker `connectUrl`; starter 3.0.0-alpha-3 does not start without it |
| network | `servers` only: it sees the gateway, Redis and the Jira mock; it does not see Keycloak, OpenBao, PostgreSQL or the broker |

The variables of the `mcp-jira` service in `docker-compose.yml` match the configuration table in the `mcp-server-template`
README (state 7f65f8f): `MCP_TLS_KEYSTORE`, `MCP_TLS_TRUSTSTORE`, `MCP_TLS_STORE_PASSWORD`,
`MCP_GATEWAY_JWKS_URI`, `MCP_GATEWAY_ISSUER`, `MCP_CONNECT_URL`, `MCP_REDIS_HOST`, `MCP_REDIS_PORT`,
`MCP_REDIS_USER`, `MCP_REDIS_PASSWORD`; `MCP_PORT` and `MCP_BIND` keep their defaults (`8443`, `0.0.0.0`).
The template does not read `MCP_GATEWAY_TLS_NAME` and `MCP_REDIS_KEY_PREFIX` (the TLS name comes from the host in
`MCP_GATEWAY_JWKS_URI`, the prefix `jti:<serverId>:` is built into the starter); they stay as information.

### Plain http backend and the `mcp-jira` image

The Jira mock listens on http (`http://jira.internal:8080`), and servers built from the `mcp-server-template` skeleton since c1258c4
refuse to start outside the `dev` profile with an http backend unless explicitly allowed. The chosen solution: the `mcp-jira` service
has `MCP_JIRA_ALLOW_PLAIN_HTTP: "true"` in `docker-compose.yml` (with a comment that it is for the mock only). Rejected: the mock over TLS
with a certificate from the internal CA and `MCP_JIRA_SSL_BUNDLE`; it would need a third server certificate,
the SAN `jira.internal` and changes in `make certs`, with no gain for testing a server that talks to Jira through a plain
HTTP client anyway. The variable works only as consent: the credential then travels in clear text and the server logs a warning.
Never set it against a real Jira. The `mcp-server-jira` server reads this variable since starter alpha-6 (checked on a64287c in `make up-full`).

The mock handles the writes used by the tools `add_comment` and `transition_issue` (shape as in the Jira DC REST API
v2 and in the `StubBackend` of the `mcp-jira` server): `POST /rest/api/2/issue/{key|id}/comment` (201; 400 without the `body` field),
`GET .../transitions` (11 To Do, 21 In Progress, 31 Done) and `POST .../transitions` (204 for 11 and 21; 400 for 31,
because it requires `resolution`, and for an unknown id). The same rules as for reads: 401 without a PAT or with a PAT outside the list,
404 for an unknown issue. Writes change nothing (the mock is stateless, reading the issue after a transition
shows the old status); the mock does not model project permissions (403). The mappings are mounted from `mocks/jira` and
WireMock reads them at startup, and the admin API is locked with a password, so after `python3 scripts/gen-jira-mock.py
mocks/jira` the `jira-mock` container must be restarted (`docker compose restart jira-mock`).

The images of the `full` profile are built by `make images` (`scripts/build-images.sh`, commits in the neighbouring repositories, without changes to their
working copies); the tags `m1-<commit>` go to `.env`. To replace the test server with the real `mcp-jira`: the service uses the image `${MCP_JIRA_IMAGE}`
from `.env` (`mcp-server-jira:m1-<commit>`, `pull_policy: never`, so the image must exist locally). Build according to the
`mcp-server-jira` README:

```bash
cd ../mcp-server-jira
export JAVA_HOME=<JDK 25>
./mvnw -B verify                       # creates mcp-app/target/mcp-jira.jar
docker build -t mcp-server-jira:dev .  # tag matching MCP_JIRA_IMAGE (the server README uses mcp-jira:dev)
cd ../mcp-platform-dev && make up-full
```

Instead of rebuilding the tag you can set `MCP_JIRA_IMAGE=mcp-jira:dev` in `.env`. The DNS alias `mcp-jira.internal`,
the certificate `certs/mcp-jira`, the Redis user `mcp-jira` and the Jira address are the same as for the test server, so the gateway
(`MCP_SERVER_JIRA_URL`) needs no changes.
On the host the real server also runs without an image (`scripts/run-dev-host.sh` in `mcp-server-jira`).

## Redis: users and prefixes (proposal for the gateway, registry and portal)

The keys are not part of the contracts. The environment assumes the prefixes below and enforces them with ACLs
(`redis/start.sh`); if a component needs others, change `redis/start.sh` and this table.

| User | Password (`.secrets/dev.env`) | Keys | Channels | Notes |
| --- | --- | --- | --- | --- |
| `gateway` | `REDIS_GATEWAY_PASSWORD` | `gw:*` (confirmations, credential cache, limits) | `credential.invalidated`: `SUBSCRIBE` only | no `PUBLISH`; command list in `redis/gateway.rules` (sets, sorted sets, `WATCH`, `EVAL`/`EVALSHA`), described in [m3.md](m3.md) |
| `broker` | `REDIS_BROKER_PASSWORD` | `broker:*`, `jti:mcp-credential-broker:*` | `credential.invalidated`: `PUBLISH` only | the broker also receives the internal JWT, so it guards `jti`; `EVAL`/`EVALSHA` for atomic lock release (Redis checks script keys against the ACL) |
| `portal-bff` | `REDIS_PORTAL_BFF_PASSWORD` | `portal:*` (Spring Session) | none | command list in `redis/portal-bff.rules`: `HSET`/`HGETALL`/`HDEL`/`DEL`/`EXPIRE`/`RENAME` and so on, without `FLUSHALL`, `KEYS`, `SCAN`, `SCRIPT`, `FUNCTION`, `MIGRATE`; `RedisIndexedSessionRepository` would also need `SADD`/`SREM`/`SMEMBERS` and keyevent channels |
| `mcp-<system>` | `REDIS_MCP_<SYSTEM>_PASSWORD` | `jti:mcp-<system>:*` | none | starter: prefix `jti:<serverId>:` |
| `admin` | `REDIS_ADMIN_PASSWORD` | all | all | dev tools and healthcheck only |

The user `default` is disabled. Component users have an explicit command list, not categories:
`GET`, `SET`, `SETNX`, `GETDEL`, `GETEX`, `MGET`, `DEL`, `UNLINK`, `EXISTS`, `EXPIRE`, `PEXPIRE`, `TTL`, `PTTL`,
`INCR`, `INCRBY`, `DECR`, `HSET`, `HGET`, `HDEL`, `HGETALL`, `HEXISTS`, `HINCRBY`, `MULTI`, `EXEC`, `DISCARD`,
`PING`, `AUTH`, `HELLO`, `CLIENT SETNAME`, `CLIENT SETINFO`, `RESET`, `QUIT` and `SUBSCRIBE`/`UNSUBSCRIBE`
(gateway) or `PUBLISH` (broker). There is no `SCAN`, `KEYS`, `RANDOMKEY`, `SCRIPT`, `FUNCTION`, and only the broker has `EVAL`:
`~prefix` patterns in the ACL do not restrict them and they would reveal key names of other components (for example `sub` in
confirmation keys). A component that needs another command (for example Lua for an atomic "check and consume"
of a confirmation) reports it to the environment owners; the change goes into `redis/start.sh` (the broker and `portal-bff` rules are in `redis/*.rules`) and into this table; on a running Redis it is applied by `scripts/redis-sync-acl.sh` (`make m2-sync`).

## Portal BFF

Client `mcp-portal-bff` (secret `KC_CLIENT_MCP_PORTAL_BFF_SECRET`), redirect
`http://localhost:18090/api/v1/auth/callback` and `http://localhost:18740/api/v1/auth/callback` (exact addresses), exchange for `mcp-credential-broker` and `mcp-gateway-admin`.
The gateway-admin stub (`https://localhost:18444`) and the broker stub (`https://localhost:18443`) require the certificate
`certs/mcp-portal-bff` (EKU clientAuth only). TLS names to verify: `MCP_BROKER_TLS_NAME`,
`MCP_GATEWAY_INTERNAL_TLS_NAME`. Step-up (`acr`) is not configured yet (STATUS.md).

## Requirements for the production IdP: session times (Entra)

Dev sets in the realm `corp` an access token of 15 min, SSO session idle time of 8 h and a maximum session time of 24 h. The production IdP (Entra ID)
should give the user the same: a break during the day (a meeting, lunch, a few hours without working with the AI client) must not force a new
login, and the next login should be required at most once a day. To agree with the Entra administrator:

- SSO session / refresh token: an idle window of at least 8 h and an absolute session lifetime of at least 24 h (in Entra this is the conditional access
  session policy: sign-in frequency and persistent browser session, not the lifetime of the token itself);
- the access token can stay short (15 min to 1 h); the client renews it with the refresh token without user involvement;
- step-up (MFA before sensitive operations, for example registering the Authenticator) must force a fresh authentication regardless of the session
  (`max_age=0` or the equivalent in the Entra authentication context), so a long session cannot replace it.

## Audit of a single request (M1 criterion)

```bash
make audit RID=<requestId>
```

Reads the logs of `mcp-gateway`, `mcp-credential-broker` and `mcp-jira` (profile `full`), selects lines with a JSON object
whose `requestId` equals `RID` (the log prefix before `{` is skipped), and prints them ordered by `ts`.
The exit code is 0 only if there are events from all three components (`component` = `gateway`,
`broker`, `server`) and each has the required audit fields (`ts`, `requestId`, `component`, `componentId`, `event`).
Components running on the host (for example a gateway from the IDE) write stdout to a file, and that file is added to the container
logs: `make audit RID=<id> LOGS="gateway.log jira.log"`. Directly: `scripts/mcpdev.py audit <id>
--logs a.log --logs b.log` (a repeatable option, events from all files are merged; with `--compose`
the container logs are added, without `--logs` only they are read). Full validation against the schema
`audit-event.schema.json` is not done (the standard library has no JSON Schema validator).

To check that Jira received a PAT and not an IdP token: `make jira-auth`.

## End-to-end tests

- `scripts/mcpdev.py` as a library (`login`, `exchange`, `put_secret`, `audit_events`, `jira_auth`) or through `make`;
- `scripts/smoke.py` as a model of environment checks;
- certificate from a foreign CA: `certs/untrusted/mcp-gateway`;
- user without permissions: `mallory`; PAT of a foreign identity: any `Bearer` other than `<login>-pat`;
- the request journal of the Jira mock (`http://localhost:18081/__admin/requests`) is on, but the admin API requires
  Basic auth `admin:$JIRA_MOCK_ADMIN_PASSWORD` (`.secrets/dev.env`, for host tools only; `mcpdev.jira_auth()`
  reads it from there). Without it an MCP server from the `servers` network could change the mappings or add a proxy stub to the
  `core` network. The proxy is also blocked (`--allow-proxy-targets 192.0.2.1`, a TEST-NET address);
- the broker and gateway-admin stubs have no journal (`--no-request-journal`), because `PUT .../secret` carries a PAT.
