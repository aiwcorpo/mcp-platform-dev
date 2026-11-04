# Claude Code and the local gateway

State (2026-10-28): the public address (M2) is `https://localhost:18745`, see the section "Through the proxy on https (M2)" at the end. The first part below describes the M1 mode (`http://localhost:18000`, `make up-full`), which still passes in `make m1`. The browser login itself
was not run; what was checked is the RFC 9728 and RFC 8414 metadata, the authorization request with PKCE and `resource`,
the redirect URI, and the rejection of a foreign redirect URI. The full instructions for a human are in the README section "M1 demo with Claude Code".

Anonymous dynamic client registration (DCR) is blocked by the default Keycloak "Trusted Hosts" policy
(`403 Host not trusted`, verified), so Claude Code gets a pre-registered public client
`claude-code` and a fixed callback port (`CLAUDE_CODE_CALLBACK_PORT` in `.env`, default 33418):

```bash
make up-full      # or a gateway running on the host on port 18000
claude mcp add --transport http --client-id claude-code --callback-port 33418 \
  jira http://localhost:18000/s/mcp-jira/mcp
```

Flow:

1. Claude Code calls the gateway without a token and gets `401` with
   `WWW-Authenticate: Bearer resource_metadata=".../.well-known/oauth-protected-resource/s/mcp-jira/mcp"`.
2. From the resource metadata it takes `authorization_servers = ["https://localhost:18746/realms/corp"]` and fetches
   `https://localhost:18746/.well-known/oauth-authorization-server/realms/corp` (in M1 mode with `make up-full`, Keycloak has
   also been served over https since 2026-10-28; CA trust: `make trust-ca`).
3. It opens the browser on the Keycloak login (login and password: `make users`), receives the code on
   `http://localhost:33418/callback`, and exchanges it with `code_verifier`.
4. The token has `aud=mcp-gateway`, `azp=claude-code`, `groups`; the gateway verifies it. There is no PAT on the workstation:
   the gateway fetches the Jira credential from the broker (first run `make pat USER=alice SYSTEM=jira` on a
   real broker).

To use another callback port, change `CLAUDE_CODE_CALLBACK_PORT` in `.env` and restart Keycloak (`make up`),
because the redirect URI in the realm is substituted at import time.

## Through the proxy on https (M2)

The public address is `https://localhost:18745` (docs/m2.md). CA trust and `NODE_EXTRA_CA_CERTS` for Node: `make trust-ca`
prints the commands for a human; without them `claude` rejects the certificate. System server:

```bash
export NODE_EXTRA_CA_CERTS=<repo directory>/certs/public-ca/ca.crt
claude mcp add --transport http --client-id claude-code --callback-port 33418 \
  jira https://localhost:18745/s/mcp-jira/mcp
```

Since M5 one address covers the whole platform (`https://localhost:18745/mcp`), and the filter is in the query, for example
`"https://localhost:18745/mcp?systems=jira&access=read"` (quoted, because the address contains `&`). The wizard in the portal builds the address
(`GET /api/v1/me/mcp-endpoint`), stores nothing, and shows a ready command
(the BFF gets `PORTAL_CLIENT_CONFIGS_CLAUDE_CODE_CLIENT_ID=claude-code` and `..._CALLBACK_PORT=33418` from `docker-compose.yml`).
The resource metadata and the 401 challenge point to the https address; the authorization server is Keycloak over https, `https://localhost:18746/realms/corp`
(a certificate from the same "dev public" CA, so Safari stores the login cookies; docs/m2.md, "Keycloak over https").
The `claude-code` client in the `corp` realm is public, with the redirect URI `http://localhost:33418/callback`.
