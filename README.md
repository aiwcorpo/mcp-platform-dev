# mcp-platform-dev

Lokalne środowisko platformy MCP (strumień ws-01): Keycloak z realmem `corp`, OpenBao, Redis, PostgreSQL,
mock Jiry, stuby kontraktowe brokera (C4) i API admina gateway'a (C5), wewnętrzne CA z certyfikatami mTLS
oraz skrypty: token użytkownika, token exchange, zapis PAT przez API brokera.

Wszystkie hasła, sekrety klientów i klucze są generowane przy pierwszym uruchomieniu do `.secrets/` i
`certs/` (oba w `.gitignore`). Są wyłącznie testowe, **NOT FOR PRODUCTION**.

## Wymagania

- Docker z `docker compose` v2 (sprawdzone na Docker 28.3, Compose 2.39),
- `make`, `bash`, `python3` >= 3.10 (tylko biblioteka standardowa), `openssl` (tylko `rand`),
- repozytorium `mcp-contracts` obok tego (`../mcp-contracts`, tag `v1.0.0`); inna ścieżka: `MCP_CONTRACTS_DIR` w `.env`.

## Start

```bash
make up        # sekrety + certyfikaty (raz), potem profil infra; czeka na healthchecki
make smoke     # testy dymne: tokeny, token exchange, mock Jiry, mTLS, OpenBao, Redis, PostgreSQL
make users     # loginy i wygenerowane hasła użytkowników testowych
```

Pierwsze `make up` na maszynie z pobranymi obrazami trwa ok. 25 s; samo pobranie obrazów zależy od łącza
(ok. 2,1 GB po rozpakowaniu, z czego Keycloak 0,75 GB).

Najczęstsze polecenia:

```bash
make claims USER=alice                         # zdekodowany token claude-code (PKCE) - aud, azp, groups
make token USER=alice                          # sam token (np. do curl -H "Authorization: Bearer ...")
make exchange USER=alice REQUESTER=mcp-gateway # token exchange do aud=mcp-credential-broker
make pat USER=alice SYSTEM=jira                # zapis PAT alice-pat przez C4 PUT /v1/connections/jira/secret
make pat USER=alice BROKER_URL=https://localhost:18100   # to samo na prawdziwym brokerze
make down | make reset | make clean
```

`make help` wypisuje wszystkie cele.

## Profile compose

| Profil | Polecenie | Co działa |
| --- | --- | --- |
| (bez profilu) | `make up-core` | Keycloak, OpenBao (+ jednorazowy `openbao-init`), Redis, PostgreSQL, mock Jiry |
| `infra` | `make up` | powyższe + stuby WireMock brokera (C4) i API admina gateway'a (C5) z `mcp-contracts` |
| `full` | `make up-full` | infrastruktura + `mcp-gateway`, `mcp-credential-broker`, `mcp-jira` z obrazów z `.env` (budują je ws-05, ws-06, ws-04) |

`up-core` jest dla strumienia, który uruchamia własny komponent na hoście i nie chce stuba pod tym samym adresem.
Compose zawsze przez `make` albo `scripts/compose.sh`: skrypt podaje `.env` i `.secrets/dev.env` jako
`--env-file`, dzięki czemu każda usługa dostaje tylko swoje sekrety.

## Dokumentacja

- [docs/porty.md](docs/porty.md): adresy i porty usług, z hosta i z sieci compose
- [docs/uzytkownicy.md](docs/uzytkownicy.md): użytkownicy, grupy C9, klienci OIDC, token exchange
- [docs/integracja.md](docs/integracja.md): jak podpiąć gateway, broker, serwer MCP, portal i testy e2e
- [docs/claude-code.md](docs/claude-code.md): podłączenie Claude Code do lokalnego gateway'a
- [STATUS.md](STATUS.md): stan względem briefu ws-01

## Układ repozytorium

```
docker-compose.yml        usługi i profile
.env                      porty i obrazy komponentów (bez sekretów)
keycloak/realm-corp.json  realm corp (import przy starcie; hasła i sekrety jako ${ZMIENNE})
mocks/jira/               mock Jiry (generowany: scripts/gen-jira-mock.py)
openbao/init.sh           KV v2, Transit, AppRole brokera
postgres/init/            bazy gateway i broker
scripts/                  init-secrets.sh, gen-certs.sh, compose.sh, mcpdev.py, smoke.py
```
