# Podpięcie komponentów

Dwa tryby pracy:

1. **Komponent na hoście** (IDE, `./mvnw spring-boot:run`) przeciw `make up` (ze stubami C4/C5) albo
   `make up-core` (bez stubów, gdy na hoście działa własny broker/gateway). Adresy z kolumny "z hosta" w
   [porty.md](porty.md).
2. **Komponent w compose** (`make up-full`): obraz z `.env` (`MCP_GATEWAY_IMAGE`, `MCP_CREDENTIAL_BROKER_IMAGE`,
   `MCP_JIRA_IMAGE`), adresy z sieci compose. `make up-full` usuwa stuby C4/C5 (mają te same aliasy DNS co
   prawdziwy broker i gateway), a `make up` usuwa komponenty profilu `full`. Usługi dostają zmienne `MCP_*`
   opisane niżej; komponent mapuje je na własną konfigurację (np. `application-dev.yml` z `${MCP_OIDC_ISSUER}`).
   Nazwy zmiennych są propozycją ws-01; zmiana jest w `docker-compose.yml` tego repo.

## Nazwy TLS brokera i gateway'a (CC-035)

Decyzja: nazwa w certyfikacie brokera i wewnętrznego endpointu gateway'a (JWKS C2, `/admin/v1`) nie jest
konwencją kontraktu, tylko parametrem konfiguracji każdego komponentu. Środowisko ustawia je w `.env`:

| Zmienna | Domyślnie | Gdzie użyta |
| --- | --- | --- |
| `MCP_BROKER_TLS_NAME` | `mcp-credential-broker.internal` | SAN certyfikatu `certs/mcp-credential-broker`, alias DNS brokera i stuba C4, `MCP_BROKER_TLS_NAME` gateway'a, weryfikacja w `make pat` |
| `MCP_GATEWAY_INTERNAL_TLS_NAME` | `mcp-gateway.internal` | SAN certyfikatu `certs/mcp-gateway`, alias DNS gateway'a i stuba C5, `MCP_GATEWAY_TLS_NAME` brokera i `mcp-jira` |

Klient ma sprawdzać tę nazwę niezależnie od adresu, pod który się łączy (np. `https://localhost:18100` z
hosta). Tak robi `make pat`: łączy się z `BROKER_URL`, a certyfikat weryfikuje względem `MCP_BROKER_TLS_NAME`;
certyfikat z inną nazwą jest odrzucany (sprawdza `make smoke`). Zmiana nazwy: `.env`, potem
`make clean && make up` (nowe certyfikaty).

## Wersja kontraktów

Stuby C4/C5 i wektory audytu pochodzą z `mcp-contracts` w wersji `MCP_CONTRACTS_REF` (`.env`, teraz `v1.1.0`).
`make up` (cel `contracts`) robi `git archive <ref>` z `../mcp-contracts` do `out/contracts/` (ignorowane
przez git) i odtwarza stuby. Kopia robocza `mcp-contracts` nie jest montowana, więc zmiany w toku w tamtym
repo nie wpływają na środowisko.

## Gateway (ws-05)

| Potrzeba | Skąd |
| --- | --- |
| walidacja tokenu C1 | JWKS `http://keycloak:8080/realms/corp/protocol/openid-connect/certs` (z hosta `http://localhost:18080/...`), `iss` `http://localhost:18080/realms/corp`, `aud` `mcp-gateway` |
| RFC 9728 `authorization_servers` | `http://localhost:18080/realms/corp` (adres widziany przez Claude Code na hoście) |
| `subjectToken` dla `resolve` (C4) | klient `mcp-gateway`, sekret `KC_CLIENT_MCP_GATEWAY_SECRET`, `audience=mcp-credential-broker` + `scope=mcp-credential-broker-audience`; innych audience gateway nie dostanie |
| klient mTLS do brokera i serwerów MCP | `certs/mcp-gateway/keystore.p12` (CN `mcp-gateway`, EKU serverAuth+clientAuth), zaufanie `certs/ca/truststore.p12` |
| nazwa brokera do weryfikacji | `MCP_BROKER_TLS_NAME` |
| sprawdzenie SAN serwera MCP (C5) | serwery mają SAN `<serverId>.internal`, np. `mcp-jira.internal` |
| własny endpoint wewnętrzny (JWKS C2) | `https://${MCP_GATEWAY_INTERNAL_TLS_NAME}:8443/.well-known/jwks.json` z certyfikatem `certs/mcp-gateway`; klucze podpisu gateway generuje sam |
| broker | `make up`: stub `https://localhost:18443` / `https://mcp-credential-broker.internal:8443`; `make up-full`: prawdziwy broker pod tym samym aliasem |
| Redis | użytkownik `gateway`, hasło `REDIS_GATEWAY_PASSWORD`, klucze `gw:*`, subskrypcja `credential.invalidated` |
| PostgreSQL | baza `gateway`, użytkownik `gateway`, hasło `GATEWAY_DB_PASSWORD` |
| audyt C8 | jedna linia JSON na zdarzenie na stdout kontenera (wtedy działa `make audit`) |

Stub brokera nie zna użytkowników: `resolve` zawsze zwraca `test-only-jira-pat-0000000000000000`, a mock
Jiry przedstawia ten PAT jako `jkowalski`. Dopiero prawdziwy broker zwróci `alice-pat` zapisany przez `make pat`.

## Credential broker (ws-06)

| Potrzeba | Skąd |
| --- | --- |
| serwer mTLS | `certs/mcp-credential-broker/keystore.p12` (SAN `${MCP_BROKER_TLS_NAME}`, `localhost`; EKU tylko serverAuth), `clientAuth` wymagany, zaufanie `certs/ca/truststore.p12` |
| rozpoznanie wywołującego (C4) | CN certyfikatu klienta: `mcp-gateway`, `mcp-portal-bff`, `mcp-offboarding` (dwa ostatnie mają EKU tylko clientAuth); obcy CA: `certs/untrusted/mcp-gateway` |
| token BFF | `aud=mcp-credential-broker`, `azp=mcp-portal-bff`, `iss` i JWKS Keycloaka jak wyżej |
| token gateway'a | wewnętrzny JWT C2 (JWKS gateway'a, nazwa TLS `MCP_GATEWAY_INTERNAL_TLS_NAME`), `aud=mcp-credential-broker` |
| wymiana `subjectToken` na system `token-exchange` | klient `mcp-credential-broker`, sekret `KC_CLIENT_MCP_CREDENTIAL_BROKER_SECRET`, `audience=opensearch` + `scope=opensearch-audience` |
| OpenBao | `http://openbao:8200` / `http://localhost:18200`; AppRole `role_id=BAO_BROKER_ROLE_ID`, `secret_id=BAO_BROKER_SECRET_ID` (ważny 7 dni, odnawia go `make up`); polityka: KV v2 `secret/data/mcp/connections/*`, Transit `transit/{encrypt,decrypt}/mcp-credential-broker`; audyt OpenBao na stdout kontenera `openbao` |
| whoami Jiry (C4.1) | `http://jira.internal:8080/rest/api/2/myself` / `http://localhost:18081/...` |
| PostgreSQL | baza `broker`, użytkownik `broker`, hasło `BROKER_DB_PASSWORD` (nie ma dostępu do bazy `gateway`) |
| Redis | użytkownik `broker`, hasło `REDIS_BROKER_PASSWORD`, klucze `broker:*` i `jti:mcp-credential-broker:*`, publikacja na `credential.invalidated` (C4.3) |

Test akceptacyjny zapisu PAT na prawdziwym brokerze (broker na hoście z portem 18100):

```bash
make pat USER=alice SYSTEM=jira BROKER_URL=https://localhost:18100              # 200, backendUser alice
make pat USER=alice SYSTEM=jira SECRET=bob-pat BROKER_URL=https://localhost:18100   # 422 identity_mismatch
```

Skrypt robi to samo co BFF: logowanie `alice` w kliencie `mcp-portal-bff` (authorization code + PKCE),
token exchange na `aud=mcp-credential-broker`, `PUT /v1/connections/{system}/secret` po mTLS z certyfikatem
`mcp-portal-bff`, z weryfikacją nazwy `MCP_BROKER_TLS_NAME`. PAT trafia do skryptu przez zmienną środowiska
(`MCP_PAT`, ustawianą przez `make` z `SECRET`), nie przez argumenty procesu, i nie jest wypisywany.

## Serwer MCP, np. `mcp-jira` (ws-02, ws-03, ws-04)

| Potrzeba | Skąd |
| --- | --- |
| serwer mTLS | `certs/mcp-jira/keystore.p12` (CN `mcp-jira`, SAN `mcp-jira.internal`, EKU tylko serverAuth); klientem jest tylko `mcp-gateway` |
| walidacja C2 | JWKS gateway'a `https://${MCP_GATEWAY_INTERNAL_TLS_NAME}:8443/.well-known/jwks.json` (nazwa TLS w `MCP_GATEWAY_TLS_NAME`), `iss` `https://mcp-gateway.internal`; do testów bez gateway'a: klucze i wektory z `mcp-contracts/test-vectors` |
| replay `jti` (C2) | Redis, użytkownik `mcp-jira`, hasło `REDIS_MCP_JIRA_PASSWORD`, klucze tylko `jti:mcp-jira:*` |
| Jira | `http://jira.internal:8080`, poświadczenie z `X-MCP-Credential` (`Bearer <PAT>`) |
| sieć | tylko `servers`: widzi gateway, Redis i mock Jiry; nie widzi Keycloaka, OpenBao, PostgreSQL ani brokera |

## Redis: użytkownicy i prefiksy (propozycja ws-01 dla ws-02, ws-05, ws-06)

Klucze nie są w kontraktach. Środowisko zakłada poniższe prefiksy i egzekwuje je ACL-ami
(`redis/start.sh`); jeśli strumień potrzebuje innych, zmienia się `redis/start.sh` i ta tabela.

| Użytkownik | Hasło (`.secrets/dev.env`) | Klucze | Kanały | Uwagi |
| --- | --- | --- | --- | --- |
| `gateway` | `REDIS_GATEWAY_PASSWORD` | `gw:*` (potwierdzenia C11, cache poświadczeń, limity) | `credential.invalidated`: tylko `SUBSCRIBE` | bez `PUBLISH` |
| `broker` | `REDIS_BROKER_PASSWORD` | `broker:*`, `jti:mcp-credential-broker:*` | `credential.invalidated`: tylko `PUBLISH` | broker też odbiera C2, więc pilnuje `jti` |
| `mcp-<system>` | `REDIS_MCP_<SYSTEM>_PASSWORD` | `jti:mcp-<system>:*` | brak | starter (ws-02): prefiks `jti:<serverId>:` |
| `admin` | `REDIS_ADMIN_PASSWORD` | wszystkie | wszystkie | tylko narzędzia dev i healthcheck |

Użytkownik `default` jest wyłączony. Wszyscy poza `admin` mają odebrane komendy z kategorii `@dangerous`
(`FLUSHALL`, `KEYS`, `CONFIG` itd.).

## Portal BFF (ws-07)

Klient `mcp-portal-bff` (sekret `KC_CLIENT_MCP_PORTAL_BFF_SECRET`), redirect
`http://localhost:18090/login/oauth2/code/*`, wymiana na `mcp-credential-broker` i `mcp-gateway-admin`.
Stub C5 (`https://localhost:18444`) i C4 (`https://localhost:18443`) wymagają certyfikatu
`certs/mcp-portal-bff` (EKU tylko clientAuth). Nazwy TLS do weryfikacji: `MCP_BROKER_TLS_NAME`,
`MCP_GATEWAY_INTERNAL_TLS_NAME`. Step-up (`acr`) jeszcze nie jest skonfigurowany (STATUS.md).

## Audyt jednego żądania (kryterium M1)

```bash
make audit RID=<requestId>
```

Czyta logi `mcp-gateway`, `mcp-credential-broker` i `mcp-jira` (profil `full`), wybiera linie z obiektem JSON,
którego `requestId` jest równy `RID` (prefiks logu przed `{` jest pomijany), i wypisuje je według `ts`.
Kod wyjścia 0 tylko wtedy, gdy są zdarzenia od wszystkich trzech komponentów (`component` = `gateway`,
`broker`, `server`) i każde ma wymagane pola C8 (`ts`, `requestId`, `component`, `componentId`, `event`).
`--logs <plik>` w `scripts/mcpdev.py audit` czyta logi z pliku. Pełna walidacja schematem
`audit-event.schema.json` nie jest robiona (brak walidatora JSON Schema w bibliotece standardowej).

Do sprawdzenia, że do Jiry dotarł PAT, a nie token z IdP: `make jira-auth`.

## Testy e2e (ws-10)

- `scripts/mcpdev.py` jako biblioteka (`login`, `exchange`, `put_secret`, `audit_events`, `jira_auth`) albo przez `make`;
- `scripts/smoke.py` jako wzór sprawdzeń środowiska;
- certyfikat obcego CA: `certs/untrusted/mcp-gateway`;
- użytkownik bez uprawnień: `mallory`; PAT cudzej tożsamości: dowolny `Bearer` inny niż `<login>-pat`;
- dziennik żądań mocka Jiry (`http://localhost:18081/__admin/requests`) jest celowo włączony i bez
  uwierzytelnienia; stuby C4/C5 nie mają dziennika (`--no-request-journal`), bo `PUT .../secret` niesie PAT.
