# mcp-platform-dev

Lokalne środowisko platformy MCP (strumień ws-01): Keycloak z realmem `corp`, OpenBao, Redis, PostgreSQL,
mock Jiry, stuby kontraktowe brokera (C4) i API admina gateway'a (C5), wewnętrzne CA z certyfikatami mTLS
oraz skrypty: token użytkownika, token exchange, zapis PAT przez API brokera.

Wszystkie hasła, sekrety klientów i klucze są generowane przy pierwszym uruchomieniu do `.secrets/` i
`certs/` (oba w `.gitignore`). Są wyłącznie testowe, **NOT FOR PRODUCTION**.

## Wymagania

- Docker z `docker compose` v2 (sprawdzone na Docker 28.3, Compose 2.39),
- `make`, `bash`, `python3` >= 3.10 (tylko biblioteka standardowa), `openssl` (tylko `rand`),
- `git` i repozytorium `mcp-contracts` obok tego (`../mcp-contracts`); stuby C4/C5 są wyciągane z tagu
  `MCP_CONTRACTS_REF` (teraz `v1.1.0`) przez `git archive` do `out/contracts/`, kopia robocza nie jest używana.
  Inna ścieżka: `MCP_CONTRACTS_DIR` w `.env`.

## Start

```bash
make up        # sekrety + certyfikaty (raz), kontrakty z MCP_CONTRACTS_REF, profil infra; czeka na healthchecki
make smoke     # testy dymne: tokeny, token exchange, mock Jiry, mTLS, OpenBao, Redis ACL, PostgreSQL, sieci
make users     # loginy i wygenerowane hasła użytkowników testowych
```

Pierwsze `make up` na maszynie z pobranymi obrazami trwa ok. 20 s; samo pobranie obrazów zależy od łącza
(ok. 2,1 GB po rozpakowaniu, z czego Keycloak 0,75 GB).

Najczęstsze polecenia:

```bash
make claims USER=alice                         # zdekodowany token claude-code (PKCE) - aud, azp, groups
make token USER=alice                          # sam token (np. do curl -H "Authorization: Bearer ...")
make exchange USER=alice REQUESTER=mcp-gateway # token exchange do aud=mcp-credential-broker
make pat USER=alice SYSTEM=jira                # zapis PAT alice-pat przez C4 PUT /v1/connections/jira/secret
make pat USER=alice BROKER_URL=https://localhost:18100   # to samo na prawdziwym brokerze
make audit RID=<requestId> [LOGS="a.log b.log"]  # zdarzenia C8 jednego żądania (kontenery + logi komponentów z hosta)
make jira-auth                                 # jakie poświadczenia dotarły do mocka Jiry
make m1                                        # scenariusze M1 (F1 A1 A2 A3 A9 A9b A15); A15 robi docker stop/start kontenera brokera (stos bez brokera, dopoki nie zdrowy)
make images                                    # obrazy gateway, broker, mcp-jira z commitów repozytoriów obok
make images-m2 | make up-m2                    # M2: proxy (jedna domena), BFF, web, serwery ws-04; docs/m2.md
make proxy-test | make mocks-test | make m2    # testy proxy, mocków i scenariusz M2; m2 zmienia stan stosu (patrz docs/m2.md)
make m3 | make otp USER=alice | make reset-authenticator USER=alice | make registry-refresh   # M3 (docs/m3.md): scenariusz Authenticatora (zmienia stan: kasuje Authenticator alice), kod OTP logowania, reset rejestracji, odswiezenie manifestow
make start-test | make e2e-prod-test           # compose start po usunięciu zadań init (osobny projekt); XH i lista 250 narzędzi na gatewayu profilu e2e-prod (docs/m6.md)
make openbao-persist-test                      # trwałość OpenBao (dev) na jednorazowej instancji; `make openbao-migrate` to jednorazowe przełączenie działającego stosu (docs/m5.md)
make m4 | make m4-test | make openbao-test     # M4 (docs/m4.md): kontrole infrastruktury (czesc po przelaczeniu SKIP), testy jednostkowe offboardingu i rejestru, init OpenBao w izolacji
make offboard USER=<login> [DRY=1]              # IdP (grupy, wylogowanie), purge brokera i gateway'a (CN mcp-offboarding), powtorzone; odmawia kont wspolnych
make registry-register | make m4-switch         # PUT manifestow certyfikatem kazdego serwera (CC-161); przelaczenie stosu na M4 (CHECK=1: tylko warunki wstepne)
make trust-ca                                   # drukuje polecenia zaufania do CA https proxy (https://localhost:18745)
make down | make reset | make clean
```

`make help` wypisuje wszystkie cele.

`make ps` pokazuje kontenery projektu (także zakończone). `make smoke` jest TYLKO DO ODCZYTU: sprawdza zdrowie kontenerów, claimy i token exchange na jednorazowym koncie Keycloaka (tworzonym i usuwanym,
konta alice, bob, carol i mallory tylko czytane przez API admina, nie logowane), mock Jiry, mTLS brokera (odmowy certyfikatów na działającym porcie), OpenBao (AppRole, klucz Transit, audyt),
ACL Redisa, PostgreSQL, sieci, certyfikaty i narzędzie audytu. Poza kontem jednorazowym zostaje po przebiegu marker purge w Redisie (TTL 30 min) i dwa zdarzenia `admin_change` w audycie. Oczekiwany wynik to 78 linii `PASS` i na końcu `all checks passed` (kod 0); inaczej lista `FAIL` i kod 1.
Kontrole, które zapisują (klucze Redisa z TTL, ścieżka KV w OpenBao, mapping mocka Jiry, złe hasło, anonimowa rejestracja klienta, POST-y do admin API mocka), są w `make smoke-write CONFIRM=1`.

## Profile compose

| Profil | Polecenie | Co działa |
| --- | --- | --- |
| (bez profilu) | `make up-core` | Keycloak, OpenBao (trwały, wolumen `openbao-data`; `openbao-unseal`; jednorazowy `openbao-init`), Redis, PostgreSQL, mock Jiry |
| `infra` | `make up` | powyższe + stuby WireMock brokera (C4) i API admina gateway'a (C5) z `mcp-contracts` `MCP_CONTRACTS_REF` |
| `full` | `make up-full` | infrastruktura + `mcp-gateway`, `mcp-credential-broker`, `mcp-jira` z obrazów z `.env` (`make images` buduje je z commitów repozytoriów obok); stuby są usuwane |

Dwa profile opcjonalne (M6, [docs/m6.md](docs/m6.md)), poza domyślnym stosem i niewłączane przez `make up*`:

| Profil | Polecenie | Co daje |
| --- | --- | --- |
| `bulk` | `make bulk-on CONFIRM=1` / `make bulk-off CONFIRM=1` (bez `CONFIRM=1` kontrole i plan) | na czas testu G9 stand-in ze 250 narzędziami w miejsce `mcp-confluence` (na polecenie koordynatora, zmienia stos) |
| `adapter` | `docker-compose.adapter.yml` (nie przez `make`) | demo Jiry przez adapter ws-13 w miejsce natywnego `mcp-jira`; wyłączony, opis w docs/m6.md punkt 9 |
| `e2e-prod` | `make e2e-prod-up`, `make e2e-prod-test`, `make e2e-prod-down` | osobny projekt: gateway bez `devstack`, broker i serwer MCP stand-in, dla XH; głównego stosu nie rusza |

`up-core` jest dla strumienia, który uruchamia własny komponent na hoście i nie chce stuba pod tym samym adresem.
Compose zawsze przez `make` albo `scripts/compose.sh`: skrypt podaje `.env` i `.secrets/dev.env` jako
`--env-file`, dzięki czemu każda usługa dostaje tylko swoje sekrety.

## Demo M1 z Claude Code

Stan: `make up-full` uruchamia prawdziwy gateway, broker i `mcp-jira`; `make m1` przechodzi F1, A1, A2, A3, A9 i A15
(patrz [STATUS.md](STATUS.md)). Logowania w przeglądarce nie da się zautomatyzować, więc ta część jest dla człowieka.
Sprawdzone bez przeglądarki: nagłówek 401 z `resource_metadata`, metadane RFC 9728 gateway'a
(`authorization_servers = https://localhost:18746/realms/corp`), metadane RFC 8414 Keycloaka, żądanie autoryzacji
klienta `claude-code` z PKCE i `resource` (formularz logowania), odrzucenie obcego redirect URI (400).

Anonimowa dynamiczna rejestracja klienta (DCR) jest w tym realmie zablokowana (`403`), więc Claude Code używa
zarejestrowanego klienta publicznego `claude-code` ze stałym portem callbacku 33418.

1. Stos i PAT alice (PAT jest zapisany w OpenBao po `make m1`; po `make reset` zapisz go ponownie):

   ```bash
   make up-full
   make pat USER=alice SYSTEM=jira BROKER_URL=https://localhost:18100   # 200 connected, backendUser alice
   make users                                                          # hasło alice dla ekranu logowania
   ```

2. Dodanie serwera w trybie M1, wprost do gateway'a na `:18000` (Claude Code 2.1.x; port 33418 musi być wolny na hoście).
   Po M2 adresem publicznym jest `https://localhost:18745` i obowiązuje komenda z
   [docs/claude-code.md](docs/claude-code.md) (ten sam `--client-id claude-code`, plus zaufanie do CA):

   ```bash
   claude mcp add --transport http --client-id claude-code --callback-port 33418 \
     jira http://localhost:18000/s/mcp-jira/mcp
   ```

3. W Claude Code: `/mcp`, wybierz `jira`, `Authenticate`. Otworzy się przeglądarka z logowaniem Keycloaka
   (realm `corp`); zaloguj się jako `alice` z hasłem z `make users`. Po powrocie na `localhost:33418/callback`
   `/mcp` pokazuje `jira` jako connected.

4. Prompt testowy: `pobierz ABC-1`. Oczekiwany wynik: wywołanie narzędzia `jira__get_issue` i dane zgłoszenia ABC-1
   z mocka ("Login page returns 500" w tytule).

5. Kontrola po stronie platformy:

   ```bash
   make jira-auth      # do mocka dotarł wyłącznie "PAT alice-pat", nigdy token z IdP
   RID=$(docker logs mcp-dev-mcp-gateway-1 2>&1 | grep '"event":"tool_call"' | tail -1 | sed -E 's/.*"requestId":"([^"]+)".*/\1/')
   make audit RID=$RID # zdarzenia gateway, broker i server jednego requestId, "missing: none"
   ```

Na stacji nie ma żadnego PAT: token Keycloaka ma `aud=mcp-gateway`, a poświadczenie do Jiry gateway pobiera z brokera.
Inny użytkownik w Claude Code: `/mcp`, `jira`, `Clear authentication`, ponowne logowanie.

## Dokumentacja

- [docs/porty.md](docs/porty.md): adresy i porty usług, z hosta i z sieci compose
- [docs/uzytkownicy.md](docs/uzytkownicy.md): użytkownicy, grupy C9, klienci OIDC, token exchange
- [docs/m4.md](docs/m4.md): certyfikaty CI per serwer, klucze `dev-`, token exchange, konto serwisowe, offboarding, przełączenie M4
- [docs/integracja.md](docs/integracja.md): jak podpiąć gateway, broker, serwer MCP, portal i testy e2e
- [docs/claude-code.md](docs/claude-code.md): podłączenie Claude Code do lokalnego gateway'a
- [STATUS.md](STATUS.md): stan względem briefu ws-01

## Układ repozytorium

```
docker-compose.yml        usługi i profile
.env                      porty i obrazy komponentów (bez sekretów)
keycloak/realm-corp.json  realm corp (import przy starcie; hasła i sekrety jako ${ZMIENNE})
mocks/jira/               mock Jiry (generowany: scripts/gen-jira-mock.py)
openbao/                  init.sh (KV v2, Transit, AppRole brokera), server.hcl (raft na wolumenie), unseal.sh (init i unseal w dev), audit.hcl (audyt na stdout)
redis/start.sh            użytkownicy i ACL Redis per komponent
postgres/init/            bazy gateway i broker
scripts/                  init-secrets.sh, gen-certs.sh, fetch-contracts.sh, compose.sh, mcpdev.py, smoke.py
out/                      (generowane) kontrakty wyciągnięte z MCP_CONTRACTS_REF
```
