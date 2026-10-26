# Podpięcie komponentów

Dwa tryby pracy:

1. **Komponent na hoście** (IDE, `./mvnw spring-boot:run`) przeciw `make up` albo `make up-core`.
   Adresy z kolumny "z hosta" w [porty.md](porty.md).
2. **Komponent w compose** (profil `full`): obraz z `.env` (`MCP_GATEWAY_IMAGE`, `MCP_CREDENTIAL_BROKER_IMAGE`,
   `MCP_JIRA_IMAGE`), adresy z sieci compose. Usługi w `docker-compose.yml` dostają zmienne `MCP_*`
   opisane niżej; komponent mapuje je na własną konfigurację (np. `application-dev.yml` z
   `${MCP_OIDC_ISSUER}`). Nazwy zmiennych są propozycją ws-01; jeśli komponent potrzebuje innych, zmiana
   jest w `docker-compose.yml` tego repo.

## Gateway (ws-05)

| Potrzeba | Skąd |
| --- | --- |
| walidacja tokenu C1 | JWKS `http://keycloak:8080/realms/corp/protocol/openid-connect/certs` (z hosta `http://localhost:18080/...`), `iss` `http://localhost:18080/realms/corp`, `aud` `mcp-gateway` |
| RFC 9728 `authorization_servers` | `http://localhost:18080/realms/corp` (adres widziany przez Claude Code na hoście) |
| token exchange dla systemów `token-exchange` | klient `mcp-gateway`, sekret `KC_CLIENT_MCP_GATEWAY_SECRET`, parametry `audience` + `scope=<audience>-audience` ([uzytkownicy.md](uzytkownicy.md)) |
| klient mTLS do brokera i serwerów MCP | `certs/mcp-gateway/keystore.p12` (CN `mcp-gateway`), zaufanie `certs/ca/truststore.p12` |
| sprawdzenie SAN serwera MCP (C5) | serwery mają SAN `<serverId>.internal`, np. `mcp-jira.internal` |
| broker | profil `infra`: stub `https://localhost:18443` / `https://mcp-credential-broker.internal:8443`; `full`: prawdziwy broker pod tym samym aliasem |
| Redis | `redis:6379` / `localhost:16379`, hasło `REDIS_PASSWORD` |
| PostgreSQL | baza `gateway`, użytkownik `gateway`, hasło `GATEWAY_DB_PASSWORD` |
| JWKS wewnętrznego JWT (C2) | gateway generuje własne klucze; środowisko zakłada wystawienie `https://mcp-gateway.internal:8443/.well-known/jwks.json` z certyfikatem `certs/mcp-gateway` |

Stub brokera nie zna użytkowników: `resolve` zawsze zwraca `test-only-jira-pat-0000000000000000`, a mock
Jiry przedstawia ten PAT jako `jkowalski`. Dopiero prawdziwy broker zwróci `alice-pat` zapisany przez `make pat`.

## Credential broker (ws-06)

| Potrzeba | Skąd |
| --- | --- |
| serwer mTLS | `certs/mcp-credential-broker/keystore.p12` (SAN `mcp-credential-broker.internal`, `localhost`), `clientAuth` wymagany, zaufanie `certs/ca/truststore.p12` |
| rozpoznanie wywołującego (C4) | CN certyfikatu klienta: `mcp-gateway`, `mcp-portal-bff`, `mcp-offboarding`; obcy CA: `certs/untrusted/mcp-gateway` |
| token BFF | `aud=mcp-credential-broker`, `azp=mcp-portal-bff`, ten sam `iss` co wyżej (JWKS Keycloak) |
| token gateway'a | wewnętrzny JWT C2 (JWKS gateway'a), `aud=mcp-credential-broker` |
| OpenBao | `http://openbao:8200` / `http://localhost:18200`; AppRole `role_id=BAO_BROKER_ROLE_ID`, `secret_id=BAO_BROKER_SECRET_ID`; polityka: KV v2 `secret/data/mcp/connections/*`, Transit `transit/{encrypt,decrypt}/mcp-credential-broker` |
| whoami Jiry (C4.1) | `http://jira.internal:8080/rest/api/2/myself` / `http://localhost:18081/...` |
| PostgreSQL | baza `broker`, użytkownik `broker`, hasło `BROKER_DB_PASSWORD` (nie ma dostępu do bazy `gateway`) |
| Redis (C4.3 `credential.invalidated`) | jak gateway |

Test akceptacyjny zapisu PAT na prawdziwym brokerze (broker na hoście z portem 18100):

```bash
make pat USER=alice SYSTEM=jira BROKER_URL=https://localhost:18100           # 200, backendUser alice
make pat USER=alice SYSTEM=jira SECRET=bob-pat BROKER_URL=https://localhost:18100  # 422 identity_mismatch
```

Skrypt robi to samo co BFF: logowanie `alice` w kliencie `mcp-portal-bff` (authorization code + PKCE),
token exchange na `aud=mcp-credential-broker`, `PUT /v1/connections/{system}/secret` po mTLS z certyfikatem
`mcp-portal-bff`. Wartość PAT nie jest wypisywana.

## Serwer MCP, np. `mcp-jira` (ws-03, ws-04)

| Potrzeba | Skąd |
| --- | --- |
| serwer mTLS | `certs/mcp-jira/keystore.p12` (CN `mcp-jira`, SAN `mcp-jira.internal`); klientem jest tylko `mcp-gateway` |
| walidacja C2 | JWKS gateway'a (`https://mcp-gateway.internal:8443/.well-known/jwks.json`), `iss` `https://mcp-gateway.internal`; do testów bez gateway'a: klucze i wektory z `mcp-contracts/test-vectors` |
| replay `jti` | Redis jak wyżej |
| Jira | `http://jira.internal:8080` / `http://localhost:18081`, poświadczenie z `X-MCP-Credential` (`Bearer <PAT>`) |

## Portal BFF (ws-07)

Klient `mcp-portal-bff` (sekret `KC_CLIENT_MCP_PORTAL_BFF_SECRET`), redirect
`http://localhost:18090/login/oauth2/code/*`, wymiana na `mcp-credential-broker` i `mcp-gateway-admin`.
Stub C5 (`https://localhost:18444`) i C4 (`https://localhost:18443`) wymagają certyfikatu
`certs/mcp-portal-bff`. Step-up (`acr`) jeszcze nie jest skonfigurowany (STATUS.md).

## Testy e2e (ws-10)

- `scripts/mcpdev.py` jako biblioteka (`login`, `exchange`, `put_secret`) albo przez `make`;
- `scripts/smoke.py` jako wzór sprawdzeń środowiska;
- certyfikat obcego CA: `certs/untrusted/mcp-gateway`;
- użytkownik bez uprawnień: `mallory`; PAT cudzej tożsamości: dowolny `Bearer` inny niż `<login>-pat`.
