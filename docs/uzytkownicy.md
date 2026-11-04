# Users, groups and clients (realm `corp`)

Source: `keycloak/realm-corp.json`, imported on every start of the container (Keycloak data is not
persistent, so the realm always matches the file). Passwords are in `.secrets/dev.env`; `make users` prints them.

## Users

| Login | Groups | Purpose |
| --- | --- | --- |
| `alice` | `mcp-jira-read`, `mcp-jira-write`, `mcp-jira-destructive`, `mcp-jira-proj-ABC`, `mcp-confluence-read`, `mcp-confluence-space-ENG`, `mcp-gitlab-read`, `mcp-gitlab-write`, `mcp-gitlab-grp-team-a`, `mcp-opensearch-read`, `mcp-opensearch-idx-logs-team-a`, `mcp-servicedesk-read`, `mcp-servicedesk-write`, `mcp-servicedesk-queue-IT`, `mcp-client-claude-code` | main demo path |
| `bob` | `mcp-jira-read`, `mcp-jira-proj-ABC`, `mcp-opensearch-read`, `mcp-opensearch-idx-logs-team-a`, `mcp-servicedesk-read`, `mcp-servicedesk-queue-FIN`, `mcp-client-claude-code` | read only |
| `carol` | `mcp-platform-admin`, `mcp-client-claude-code` | platform admin |
| `mallory` | none | no permissions |

E-mail addresses are `<login>@corp.test`. Groups without users, prepared for scope tests:
`mcp-jira-proj-DEF`, `mcp-jira-proj-HR`, `mcp-confluence-write`, `mcp-opensearch-idx-logs-team-b`.

Realm session times: access token 15 min (`accessTokenLifespan` 900), SSO session expires after 8 h idle
(`ssoSessionIdleTimeout` 28800) and at the latest 24 h after login (`ssoSessionMaxLifespan` 86400). Earlier the values were
1 h and 10 h, so a break during the day forced a new login. Step-up (level `mfa`, OTP code) does not depend on these
values: level 2 has `loa-max-age` 0 and the BFF sends `max_age=0`, so every step-up asks for the password and OTP again.
Level 1 (password only) has `loa-max-age` 86400 (24 h, was 36000) so that it does not shorten the session below `ssoSessionMaxLifespan`; see the section on sessions in `docs/m5.md`.

The `groups` claim contains names without a path (`mcp-jira-read`, not `/mcp-jira-read`): the mapper
`oidc-group-membership-mapper` with `full.path=false` in the client scope `groups`.

## OIDC clients

| Client | Type | Flows | Notes |
| --- | --- | --- | --- |
| `claude-code` | public | authorization code + PKCE S256 (enforced) | redirect `http://localhost:33418/callback` and `http://127.0.0.1:33418/callback`; always `aud=mcp-gateway`; no ROPC |
| `mcp-gateway` | confidential | token exchange only | exchanges a token with `aud=mcp-gateway` only for `mcp-credential-broker` (`subjectToken` for `resolve`, broker contract) |
| `mcp-portal-bff` | confidential | authorization code (+PKCE), token exchange | redirect `http://localhost:18090/api/v1/auth/callback` and `http://localhost:18740/api/v1/auth/callback` (exact addresses); exchanges for `mcp-credential-broker` or `mcp-gateway-admin` |
| `mcp-credential-broker` | confidential | token exchange only | `aud` of broker-contract tokens; exchanges `subjectToken` (broker aud) for `opensearch` for systems of type `token-exchange` |
| `mcp-gateway-admin` | confidential | none | used only as `aud` |
| `opensearch` | confidential | none | used only as `aud` (credential type `token-exchange`) |

Client secrets: `KC_CLIENT_*_SECRET` in `.secrets/dev.env`.

The `claude-code` token contains `iss`, `sub`, `aud`, `exp`, `iat`, `azp`, `preferred_username`, `groups`,
`acr`, `auth_time` (identity contract); lifetime 15 min. The client scope `roles` is not assigned, so the token has no
`realm_access` and no `aud=account`.

## Token exchange (standard, RFC 8693)

The exchange chain follows the broker contract: the gateway exchanges the AI client token for `aud=mcp-credential-broker` and passes it
in `resolve` as `subjectToken`; only the broker exchanges it for the audience of the target system (`opensearch`).
The BFF exchanges its session token for `mcp-credential-broker` or `mcp-gateway-admin`.

| Exchanging client | Input token | Allowed `audience` |
| --- | --- | --- |
| `mcp-gateway` | `claude-code` (`aud=mcp-gateway`) | `mcp-credential-broker` |
| `mcp-portal-bff` | its own session token | `mcp-credential-broker`, `mcp-gateway-admin` |
| `mcp-credential-broker` | `aud=mcp-credential-broker` | `opensearch` |

Target audiences are in optional client scopes (`mcp-credential-broker-audience`,
`mcp-gateway-admin-audience`, `opensearch-audience`). A regular login token does not have them; the exchange must pass
both `audience` and the matching scope:

```bash
curl -s http://localhost:18080/realms/corp/protocol/openid-connect/token \
  -d grant_type=urn:ietf:params:oauth:grant-type:token-exchange \
  -d client_id=mcp-gateway -d client_secret="$KC_CLIENT_MCP_GATEWAY_SECRET" \
  -d subject_token="$USER_TOKEN" \
  -d subject_token_type=urn:ietf:params:oauth:token-type:access_token \
  -d audience=mcp-credential-broker -d scope=mcp-credential-broker-audience
```

Without `scope`, Keycloak 26.8 answers `Requested audience not available`. Result: `aud=mcp-credential-broker`,
the same `sub`, `azp` = the exchanging client. Checked in `make smoke`:

- works: gateway -> broker, BFF -> broker, broker (token from the gateway) -> `opensearch`;
- denied: gateway -> `opensearch`, gateway -> `mcp-gateway-admin`, gateway with the broker token -> `opensearch`,
  `claude-code` (public client) -> anything, BFF with a `claude-code` token, BFF with a token issued to the gateway,
  `claude-code` asking for the broker scope at login;
- an exchange by the gateway without `audience` gives a token without any component audience (useless for recipients).

Anonymous dynamic client registration is blocked explicitly: the realm declares the "Trusted Hosts" policy
with an empty host list (`components` in `realm-corp.json`).

## Jira mock: PATs and identities

| `Authorization` | `GET /rest/api/2/myself` returns `name` |
| --- | --- |
| `Bearer alice-pat` / `bob-pat` / `carol-pat` / `mallory-pat` | `alice` / `bob` / `carol` / `mallory` |
| `Bearer test-only-jira-pat-0000000000000000` | `jkowalski` (value from the `resolve` stub in `mcp-contracts`) |
| any other `Bearer ...` | `someone-else` (for the `identity_mismatch` test) |
| none | `401` |

Data: projects `ABC`, `DEF`, `HR` (category `Restricted`); issues `ABC-1` (links to `HR-1` and `DEF-1`),
`ABC-2`, `DEF-1`, `DEF-5`, `HR-1`; `ABC-3` moved to `DEF` (the response carries the key `DEF-5`);
`/rest/api/2/search` (GET and POST) recognizes `project = ABC|DEF|HR` in JQL. Data endpoints require
one of the known PATs from the table. Every other credential, including a Keycloak token (token passthrough,
invariant 2), a foreign PAT and `Basic`, gets `401`. `make jira-auth` shows from the mock request log (admin API with the password from `.secrets/dev.env`) which
credential reached Jira (the name of a known PAT, a marked JWT with `iss`/`aud`, others only by length).
The mock does not check per-user permissions.

M3: `alice`, `bob` and `carol` have a login OTP (`acr` level `mfa`, see [m3.md](m3.md)); `mallory` does not. `alice` has `mcp-jira-destructive`
(without it `delete_issue` ends with a Cedar denial before the TOTP code), `bob` does not (the denial scenario).
