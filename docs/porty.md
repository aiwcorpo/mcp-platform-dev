# Ports and addresses

Host ports are set in `.env` and bind only to `127.0.0.1`. Compose has two networks:

- `mcp-dev-core`: Keycloak, OpenBao, PostgreSQL, Redis, the Jira mock, the broker and gateway-admin stubs, broker, gateway;
- `mcp-dev-servers`: gateway, MCP servers, Redis, the Jira mock. An MCP server does not see Keycloak, OpenBao,
  PostgreSQL or the broker (checked by `make smoke`).

Services have service names and `*.internal` aliases.

| Service | From the host | From the compose network | Notes |
| --- | --- | --- | --- |
| Keycloak (realm `corp`, public address) | `https://localhost:18746` (`KEYCLOAK_TLS_PORT`) | `https://keycloak:8443` (only for the name `localhost`, practically unneeded) | `iss` is always `https://localhost:18746/realms/corp`; certificate from the CA "dev public"; browsers and Claude Code |
| Keycloak (http) | `http://localhost:18080` | `http://keycloak:8080` | container backchannel (JWKS, token) and scripts (`make token`, admin API); login forms work from here, but `iss` and the addresses in discovery are https |
| Keycloak admin console | `https://localhost:18746/admin` | | login from `make admin-password` |
| OpenBao (dev) | `http://localhost:18200` | `http://openbao:8200` | stack token: `BAO_ROOT_TOKEN` in `.secrets/dev.env`; unseal key and init token in `.secrets/openbao/` (dev only); data on the volume `openbao-data` (survives Stop/Start); audit on the container stdout |
| Redis | `localhost:16379` | `redis:6379` | ACL per component, see [integracja.md](integracja.md#redis-users-and-prefixes-proposal-for-the-gateway-registry-and-portal) |
| PostgreSQL | `localhost:15432` | `postgres:5432` | databases `gateway` and `broker`, users with the same names |
| Jira mock (HTTP) | `http://localhost:18081` | `http://jira.internal:8080` | PAT as `Authorization: Bearer <pat>`; `/__admin` with Basic auth `admin:$JIRA_MOCK_ADMIN_PASSWORD` |
| Broker stub (mTLS) | `https://localhost:18443` | `https://${MCP_BROKER_TLS_NAME}:8443` | `make up`; contracts from `MCP_CONTRACTS_REF` |
| Gateway admin stub (mTLS) | `https://localhost:18444` | `https://${MCP_GATEWAY_INTERNAL_TLS_NAME}:8443` | `make up` |
| Gateway (public listener) | `http://localhost:18000` | `http://mcp-gateway.internal:8080` | only for `make m1` (mcp-e2e); MCP clients and the browser use the proxy (https `:18745`), and the advertised origin (`MCP_PUBLIC_URL`) is the proxy |
| Gateway (internal, mTLS) | `https://localhost:18445` | `https://${MCP_GATEWAY_INTERNAL_TLS_NAME}:8443` | JWKS and actuator; host port for tests only, requires a client certificate |
| Broker | `https://localhost:18100` | `https://${MCP_BROKER_TLS_NAME}:8443` | `make up-full` (the stubs are then removed) |
| `mcp-jira` | `https://localhost:18446` | `https://mcp-jira.internal:8443` | profile `full`; host port for tests only, requires the `mcp-gateway` certificate and a JWT |
| Portal (reserved) | `http://localhost:18090` | | redirect URI of the `mcp-portal-bff` client (BFF running on the host) |
| Proxy (one domain, M2) | `https://localhost:18745` (public address), `http://localhost:18740` (same routes, no TLS) | `proxy.internal:8080` | `make up-m2`; certificate from the CA "dev public" (`make trust-ca`); `/` web, `/api` BFF, `/s/` `/mcp` (M5; `/v/` up to M4) `/.well-known/oauth-protected-resource*` gateway; see [m2.md](m2.md) |
| Portal web (M2) | | `http://portal-web:8080` | static SPA server behind the proxy |
| Portal BFF (M2) | | `http://portal-bff.internal:8080` | behind the proxy, `PORTAL_PORT=8080`; network `core`, OIDC without discovery (see [m3.md](m3.md)) |
| GitLab mock (OAuth + REST) | `http://localhost:18741` | `http://gitlab.internal:8080` | the browser opens authorize (host port), the rest goes over the compose network |
| Confluence mock | `http://localhost:18742` | `http://confluence.internal:8080` | |
| Service Desk mock | `http://localhost:18743` | `http://servicedesk.internal:8080` | network `servers` |
| OpenSearch mock | `http://localhost:18744` | `http://opensearch.internal:8080` | network `servers` |
| `mcp-confluence`, `mcp-gitlab`, `mcp-servicedesk`, `mcp-opensearch` | | `https://<name>.internal:8443` | profile `m2`, no host ports |
| Policy bundle server (M3) | `http://localhost:18747` (`POLICIES_PORT`) | `http://policies.internal:8080`, `https://policies.internal:8443` (MCP servers; certificate from the internal CA) | nginx, read only; `/policies/{main,overrides}/`; networks `core` and `servers`; see [m3.md](m3.md) |
| Gateway, M5 (two instances) | instances `http://localhost:18752` and `:18753`; balancer `:18000` (`GATEWAY_PORT`), mTLS `:18445`, status `http://localhost:18750/__lb/status` | `gateway-backends.internal` (instances), `mcp-gateway.internal` and `gateway-lb` (balancer) | after `make m5-switch`; [m5.md](m5.md) |
| Broker, M5 (two instances) | instances `https://localhost:18754` and `:18755`; balancer `:18100` (`BROKER_PORT`), status `http://localhost:18751/__lb/status` | `broker-backends.internal` (instances), `mcp-credential-broker.internal` (balancer) | as above |
| Package server | `http://localhost:18756` | `packages.internal` (network `ci`) | read only; `make m5-artifacts`; [m5.md](m5.md) |
| Image registry | `localhost:18757` | `registry.internal` (network `ci`) | http on loopback, no authentication; `make m5-artifacts` |
| Profile `e2e-prod` (M6; [m6.md](m6.md)) | gateway `http://localhost:18760`, gateway admin mTLS `https://localhost:18761`, broker stand-in admin `http://localhost:18762` (`e2e-prod.env`, reserved 18760-18769) | network `mcp-e2e-prod-net` (its own), `idp-front` also in `mcp-dev-core` | only after `make e2e-prod-up`; separate project `mcp-e2e-prod` |
| Claude Code callback | `http://localhost:33418/callback` | | redirect URI of the `claude-code` client |

## Keycloak: issuer and backchannel

`KC_HOSTNAME=https://localhost:18746` with `KC_HOSTNAME_BACKCHANNEL_DYNAMIC=true`; Keycloak serves https on 8443 itself
(`KC_HTTPS_CERTIFICATE_FILE`, certificate `certs/keycloak` from the CA "dev public", SAN `localhost`) and keeps http on 8080. Consequences:

- `iss` in every token is `https://localhost:18746/realms/corp`, also when the token was issued to a container over
  `http://keycloak:8080` (`make smoke` checks this);
- discovery fetched from the compose network over http gives `jwks_uri` and `token_endpoint` under `http://keycloak:8080/...`;
- Java containers (gateway, broker) do not connect to Keycloak over https, so they do not need the CA in a truststore:
  JWKS and the token endpoint go over http to the network alias, and `iss` is only compared as a string;
- a component in a container **cannot** use `issuer-uri` with discovery (inside a container `localhost` is the container itself).
  Configuration: JWKS `http://keycloak:8080/realms/corp/protocol/openid-connect/certs`, expected `iss`
  `https://localhost:18746/realms/corp`. In Spring Boot: `spring.security.oauth2.resourceserver.jwt.jwk-set-uri`
  plus `issuer-uri` (when `jwk-set-uri` is set, Boot does not run discovery, it only checks `iss`).
- a component running on the host can use a plain `issuer-uri` (https, requires trusting `certs/public-ca/ca.crt`);
- the portal BFF (M3) does not run discovery: `PORTAL_OIDC_AUTHORIZATION_URI` is the public address (for the browser), the token, JWKS and
  logout go through `http://keycloak:8080`, and `PORTAL_OIDC_ISSUER_URI` is only compared with `iss`.

Authorization server metadata (RFC 8414) is at
`https://localhost:18746/realms/corp/.well-known/oauth-authorization-server` and in the variant with the inserted path
`https://localhost:18746/.well-known/oauth-authorization-server/realms/corp`.

## Certificates (`certs/`, generated by `make certs`)

| File | Content |
| --- | --- |
| `certs/ca/ca.crt` | internal CA (EC P-256) |
| `certs/ca/truststore.p12` | CA as PKCS12 for Java (trusted entry, `-jdktrust`); password `TLS_STORE_PASSWORD` |
| `certs/<name>/tls.crt`, `tls.key`, `chain.crt` | certificate, PKCS#8 key, certificate + CA |
| `certs/<name>/keystore.p12` | key + chain for Java; password `TLS_STORE_PASSWORD` |
| `certs/untrusted/mcp-gateway/` | CN `mcp-gateway` signed by another CA, for negative tests |
| `certs/legacy/mcp-registry-ci/` | the former shared pipeline CN (M3); since gateway 4.0.0 it gets 403, for the negative test only, mounted nowhere |
| `certs/public-ca/ca.crt`, `ca.key` | separate CA "dev public" for the public address (`scripts/gen-public-cert.sh`); name constraints: only `localhost`, `127.0.0.1`, `::1`; the key stays on the host, mounted nowhere |
| `certs/proxy/tls.crt`, `tls.key`, `chain.crt` | proxy server certificate (SAN `localhost`, `127.0.0.1`, `::1`, 397 days), mounted into the `proxy` container |
| `certs/keycloak/tls.crt`, `tls.key`, `chain.crt` | Keycloak certificate (same CA, same SAN and lifetime), mounted into `keycloak` and `idp-relay` |
| `certs/public-ca/truststore.p12` | only the CA "dev public" as PKCS12 for Java (BFF); password `TLS_STORE_PASSWORD` |

| Name | EKU | SAN |
| --- | --- | --- |
| `mcp-gateway` | serverAuth, clientAuth | `${MCP_GATEWAY_INTERNAL_TLS_NAME}`, `mcp-gateway`, `localhost`, `127.0.0.1` |
| `mcp-credential-broker` | serverAuth | `${MCP_BROKER_TLS_NAME}`, `mcp-credential-broker`, `localhost`, `127.0.0.1` |
| `mcp-jira`, `mcp-confluence`, `mcp-gitlab`, `mcp-servicedesk`, `mcp-opensearch` | serverAuth | `<serverId>.internal`, `<serverId>`, `localhost`, `127.0.0.1` |
| `mcp-portal-bff`, `mcp-offboarding` | clientAuth | `<name>` |
| `mcp-registry-ci-mcp-jira`, `-mcp-confluence`, `-mcp-gitlab`, `-mcp-servicedesk`, `-mcp-opensearch` | clientAuth | `<name>` (one pipeline certificate per server) |

All have `CN=<name>`. Only server certificates get `localhost` in the SAN, for components
running on the host; production certificates do not have it. A client should still verify the name
from configuration (`MCP_BROKER_TLS_NAME`, `MCP_GATEWAY_INTERNAL_TLS_NAME`), not the address it connects to.
The stub containers mount only their own keystore, the CA (`ca.crt`, `truststore.p12`) and a client certificate for the
healthcheck; the CA key and the keys of other identities stay on the host.
