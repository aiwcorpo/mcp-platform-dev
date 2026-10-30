# Status ws-01 (mcp-platform-dev)

Cel sesji: kamień M1 (to, czego potrzebuje walking skeleton z `00-overview.md`). Brief ws-01 nie dzieli
pracy na kamienie, więc M1 = podzbiór produktów potrzebny do demo M1; reszta briefu jest niżej jako
"poza M1". Po przeglądzie M1 (2026-10-26) poprawione znaleziska H1-H2, M1-M6, L1-L9 (tabela niżej).

## M1: zrobione

Sprawdzone `make clean && make up && make smoke`: 92 sprawdzenia, wszystkie PASS (po poprawce `audit` 93 na działającym środowisku, bez restartu); `make up` od zera ok. 20 s
przy pobranych obrazach.

| Produkt | Stan |
| --- | --- |
| Keycloak 26.8.1, realm `corp` | klienci `claude-code` (public, PKCE S256 wymuszone, bez ROPC, `aud=mcp-gateway`), `mcp-gateway`, `mcp-portal-bff`, `mcp-credential-broker`, `mcp-gateway-admin`, `opensearch`; standardowy token exchange w łańcuchu z C4 (gateway -> broker, BFF -> broker/admin, broker -> opensearch); mapper `groups` z `full.path=false`; jawna polityka DCR "Trusted Hosts" bez hostów; użytkownicy `alice`, `bob`, `carol`, `mallory` z grupami C9 |
| OpenBao 2.7.2 (dev) | KV v2 `secret/`, Transit z kluczem `mcp-credential-broker`, AppRole brokera (polityka tylko na swoje ścieżki, `secret_id` 7 dni), audyt deklaratywny na stdout |
| Redis 8.8.4 | ACL per komponent (`gateway`, `broker`, `mcp-<system>`, `admin`), `default` wyłączony, jawna lista komend (bez `SCAN`/`KEYS`/`EVAL`), prefiksy kluczy i kanał `credential.invalidated` wg `docs/integracja.md` |
| PostgreSQL 18.6 | bazy `gateway` i `broker`, osobni użytkownicy, brak dostępu krzyżowego |
| Mock Jiry (WireMock 3.13.2) | `/rest/api/2/myself` (C4.1) dla `<login>-pat`, inny PAT = inny użytkownik; dane tylko dla znanych PAT (token z IdP, cudzy PAT -> 401); projekty `ABC`, `DEF`, `HR` (`Restricted`), linki między projektami, zgłoszenie przeniesione; `make jira-auth` pokazuje, jakie poświadczenie dotarło; admin API z Basic auth, proxy zablokowane |
| Stuby C4 i C5 | z `mcp-contracts` `v1.1.0` (`MCP_CONTRACTS_REF`) przez `git archive` do `out/contracts/`, tylko HTTPS z wymaganym certyfikatem klienta, bez dziennika żądań |
| CA i mTLS | CA EC P-256; `CN=<nazwa>`; EKU per rola; SAN `<serverId>.internal` dla serwerów MCP, nazwy TLS brokera i gateway'a z `.env` (CC-035); PEM + PKCS12 dla Javy; certyfikat obcego CA do testów negatywnych |
| Skrypt zapisu PAT | `make pat USER=alice SYSTEM=jira [SECRET=...] [BROKER_URL=...]`: PKCE w kliencie BFF, token exchange, `PUT /v1/connections/{system}/secret` po mTLS z weryfikacją `MCP_BROKER_TLS_NAME`; PAT przez zmienną środowiska |
| Audyt M1 | `make audit RID=<requestId>`: zdarzenia C8 jednego żądania z logów gateway'a, brokera i `mcp-jira`, kod 0 tylko przy komplecie |
| Sieci | `core` i `servers`; serwery MCP nie widzą Keycloaka, OpenBao, PostgreSQL ani brokera |
| Makefile | `up`, `up-core`, `up-full`, `down`, `reset`, `clean`, `certs`, `secrets`, `contracts`, `seed`, `token`, `claims`, `exchange`, `pat`, `audit`, `jira-auth`, `smoke`, `logs`, `ps`, `users` |
| Profile compose | bez profilu (infrastruktura), `infra` (+ stuby), `full` (gateway, broker, `mcp-jira` z obrazów z `.env`; renderuje się, obrazów jeszcze nie ma); `up` i `up-full` wzajemnie usuwają stuby i komponenty o tych samych aliasach |
| Obrazy | przypięte tagiem i digestem (`tag@sha256:...`) |
| Mock Jiry: zapisy | `POST issue/{key}/comment`, `GET` i `POST issue/{key}/transitions` (generator `scripts/gen-jira-mock.py`), PAT jak przy odczycie (401/404), 400 jak w Jira DC; bez stanu i bez 403. Sprawdzone na osobnym WireMocku (port 18570); działający `jira-mock` wymaga restartu, żeby je załadować |
| `mcp-jira` w `full` | `MCP_JIRA_ALLOW_PLAIN_HTTP: "true"` (mock po http, tylko dev); wybór i budowa obrazu w `docs/integracja.md`. Serwer `mcp-server-jira` 79ffecd zmiennej jeszcze nie czyta |

DoD briefu, punkty dotyczące M1:

- `make token USER=alice` zwraca JWT z `groups` bez ścieżki i `azp=claude-code`: tak (`make claims`).
- token exchange `alice` -> `aud=mcp-credential-broker`: tak, przez `mcp-gateway` i przez `mcp-portal-bff`.
- mock Jiry: `whoami` dla `alice-pat` -> `alice`, dla innego -> `someone-else`: tak.
- `make up` < 3 min, healthchecki zielone: tak przy pobranych obrazach; czasu z pustym cache obrazów
  (ok. 2,1 GB) nie mierzyłem.

## Przegląd M1: znaleziska i poprawki

| Znalezisko | Co zrobione |
| --- | --- |
| H1 mock Jiry przyjmuje dowolny token | dane tylko dla znanych PAT, reszta 401; smoke: token z IdP i cudzy PAT -> 401; `make jira-auth` i `mcpdev.jira_auth()` czytają dziennik mocka (PAT po nazwie, JWT oznaczony jako passthrough) |
| H2 stuby z kopii roboczej `mcp-contracts` | `MCP_CONTRACTS_REF=v1.1.0`, `scripts/fetch-contracts.sh` (`git archive`) do `out/contracts/`; montowany katalog nie jest workdirem kontenera; stuby odtwarzane przy `make up`; smoke sprawdza ref i źródło montowania |
| M1 exchange na `opensearch` przez gateway | `opensearch-audience` i exchange przeniesione na `mcp-credential-broker`; gateway tylko `mcp-credential-broker`; testy negatywne |
| M2 wspólne aliasy stubów i komponentów | `make up-full` usuwa stuby, `make up` i `up-core` usuwają komponenty `full`; opisane w `docs/integracja.md` |
| M3 wspólne hasło Redis | ACL per komponent; prefiksy jako propozycja dla ws-02/05/06 w `docs/integracja.md`; smoke sprawdza dozwolone i zabronione operacje |
| M4 stuby montują cały `certs/` | tylko własny keystore, `ca.crt`, `truststore.p12` i certyfikat do healthchecku; smoke sprawdza brak `ca.key` |
| M5 brak narzędzia audytu | `make audit RID=...`; smoke testuje parser na zdarzeniach z wektorów `v1.1.0` |
| M6 niespójna wersja kontraktów | `MCP_CONTRACTS_REF` w `.env`, README i STATUS opisują ref |
| L1 EKU i weryfikacja nazwy | EKU per rola, `localhost` tylko w certyfikatach serwerowych; `put_secret` weryfikuje `MCP_BROKER_TLS_NAME` (decyzja CC-035) |
| L2 niejawna blokada DCR | polityki rejestracji klientów zadeklarowane w realmie, "Trusted Hosts" z pustą listą |
| L3 sekrety w argv | Redis: plik ACL z env; OpenBao: `BAO_DEV_ROOT_TOKEN_ID` i `secret_id=-` przez stdin; smoke: wszystko przez stdin; `make pat`: `SECRET` przez eksportowaną zmienną |
| L4 WireMock admin i dziennik | stuby `--no-request-journal`; dziennik mocka Jiry świadomie otwarty (opis w compose i docs) |
| L5 `|| true`, `secret_id_ttl=0`, brak audytu | idempotencja przez `secret-id/lookup`, `secret_id_ttl=168h`, audyt deklaratywny (`openbao/audit.hcl`); brak `token_bound_cidrs` uzasadniony w `init.sh` |
| L6 płaska sieć | sieci `core` i `servers`; smoke sprawdza widoczność z `servers`; profil `full` renderuje się |
| L7 exchange bez `audience` | test: token bez audience komponentu |
| L8 brakujące testy negatywne | `claude-code` nie robi exchange, BFF nie wymieni tokenu `claude-code` ani tokenu gateway'a, gateway nie dostaje `opensearch` |
| L9 drobne | komentarz `make clean`, opis `BROKER_URL` w `make help`, obrazy przypięte digestem |
| L10 brak endpointów zapisu, `project in (...)` | poza M1, niezrobione |

Przegląd bezpieczeństwa commita f0e8ae0:

| Znalezisko | Co zrobione |
| --- | --- |
| MEDIUM: admin API mocka Jiry bez uwierzytelnienia w sieciach `core` i `servers` (zmiana mapowań, proxy = SSRF do `core`) | `--admin-api-basic-auth` z generowanym `JIRA_MOCK_ADMIN_PASSWORD`, `--allow-proxy-targets 192.0.2.1`; smoke: z sieci `servers` `GET/POST /__admin/mappings` i `POST /__admin/reset` -> 401; stub proxy do `keycloak:8080` dodany z hasłem -> 500 "target proxy address is denied" |
| MEDIUM: kategorie `+@keyspace`/`+@scripting` w ACL Redis ujawniają nazwy cudzych kluczy | jawna lista komend w `redis/start.sh`, opis i zasada zgłaszania w `docs/integracja.md`; smoke: `mcp-jira` bez `SCAN`/`KEYS`/`RANDOMKEY`/`EVAL`/`SCRIPT` i bez `gw:*`, `gateway` bez `SCAN`/`KEYS` |

Integracja z brokerem ws-06 (commit c59dc82, obraz `mcp-credential-broker:dev`):

- usługa `mcp-credential-broker` w profilu `full` ma `MCP_PORTAL_URL` i `MCP_ALLOW_PLAIN_HTTP=true` (tylko dev;
  OpenBao i mock Jiry są po http); `MCP_JIRA_URL=http://jira.internal:8080` już było; `compose config` dla
  `full` przechodzi;
- broker uruchomiony sam (`up --no-deps`) na infrastrukturze z `make up`:
  `make pat USER=alice BROKER_URL=https://localhost:18100` -> 200 `connected`, `backendUser: alice`;
  `SECRET=bob-pat` -> 422 `identity_mismatch`; `mcpdev.py audit <requestId>` znajduje zdarzenie
  `connection_change` brokera w jego logach (brak gateway'a i serwera, więc kod 1, zgodnie z oczekiwaniem);
  w liniach audytu brokera nie ma wartości PAT; kontener brokera usunięty po teście.

Poprawka `audit` (zgłoszenie z testu gateway'a ws-05): `--logs` przyjmowało jeden plik, kolejne po cichu
nadpisywały poprzedni, więc audyt zgłaszał brak komponentów, których zdarzenia były w innych plikach. Teraz
`--logs` jest powtarzalne i zdarzenia ze wszystkich plików są scalane; `--compose` dokłada logi kontenerów;
`make audit RID=... LOGS="a.log b.log"` łączy logi komponentów z hosta z logami kontenerów; nieczytelny plik
daje błąd zamiast cichego pominięcia. Smoke: nowy test scalania trzech plików.

Integracja z template'em ws-03 (7f65f8f, starter 3.0.0-alpha-3): usługa `mcp-jira` w profilu `full` ma
`MCP_CONNECT_URL=http://localhost:${PORTAL_PORT}/connections?system={system}` (bez niego serwer nie wstaje;
kształt jak `connectUrl` brokera i przykład C4). Pozostałe zmienne przejrzane względem README template'u
(zgodne, opis w `docs/integracja.md`). Sprawdzone `compose --profile full config` i `make smoke`; samego
kontenera `mcp-jira` nie uruchamiałem (obraz `mcp-server-jira:dev` jeszcze nie istnieje).

## M1 w compose z prawdziwymi komponentami (2026-10-27)

`make up-full` z obrazami zbudowanymi `make images` (`scripts/build-images.sh`: `git archive` commita do katalogu
tymczasowego, `./mvnw package -DskipTests`, `docker build`; repozytoria obok tylko do odczytu):

| Komponent | Commit | Obraz |
| --- | --- | --- |
| `mcp-gateway` | d4fbf98 | `mcp-gateway:m1-d4fbf98` |
| `mcp-credential-broker` | 9a5d292 | `mcp-credential-broker:m1-9a5d292` |
| `mcp-server-jira` (starter alpha-6) | a64287c | `mcp-server-jira:m1-a64287c` |

`make m1` (`scripts/m1-compose.py`) uruchamia scenariusze z `mcp-e2e` (importowane tylko do odczytu, nic nie jest
zapisywane w tamtym repo) na kontenerach: F1, A1, A2, A3, A9 (logi gateway'a, brokera, `mcp-jira` oraz logi
wszystkich kontenerów projektu) i A15 (broker zatrzymany i uruchomiony przez `docker stop/start`). Wynik: wszystkie PASS,
w tym `mcpdev.py audit --compose` dla jednego `requestId` -> gateway, broker i server, `missing: none`, oraz zero
wystąpień `alice-pat`, tokenu alice i ciągów JWT w logach 9 kontenerów (w tym OpenBao i Keycloaka).

Zmiany w środowisku:

- `.env`: tagi obrazów jak wyżej; `GATEWAY_INTERNAL_PORT=18445`, `MCP_JIRA_PORT=18446`.
- `docker-compose.yml`: gateway dostał `MCP_PUBLIC_URL` i `MCP_ALLOW_PLAIN_HTTP` (token exchange po http do Keycloaka);
  porty hosta 18445 (gateway wewnętrzny) i 18446 (`mcp-jira`) dla testów negatywnych z hosta (A1, A3). Oba wymagają
  certyfikatu klienta `mcp-gateway`; `mcp-jira` nadal wymaga też wewnętrznego JWT.
- `make smoke` dotyczy profilu `infra` (stuby); przy `make up-full` kończy się na sprawdzeniu stubów, bo ich nie ma. Do
  profilu `full` służy `make m1`.
- Stuby `mcp-dev-broker-mock-1` i `mcp-dev-gateway-admin-mock-1` zostały zatrzymane i usunięte przez `make up-full`
  (za zgodą człowieka); wolumeny, realm i OpenBao bez zmian.

Claude Code: sekcja "Demo M1 z Claude Code" w README. Bez przeglądarki sprawdzone: 401 z `resource_metadata`, metadane
zasobu (`authorization_servers` = Keycloak), metadane RFC 8414, żądanie autoryzacji `claude-code` z PKCE i `resource`
(formularz logowania), obcy redirect URI -> 400, anonimowy DCR -> 403 (więc `--client-id claude-code
--callback-port 33418`). Samo logowanie i wywołanie narzędzia z Claude Code nie były wykonane.

## M2 (2026-10-28): jedna domena, mocki, profil m2

Opis: [docs/m2.md](docs/m2.md). Kamienie pośrednie:

| Kamień | Zakres | Stan |
| --- | --- | --- |
| M2.1 | reverse proxy nginx (`proxy/`), `make proxy-test` (106 sprawdzeń, dodatnie i ujemne dla każdej reguły CC-089) | zrobione |
| M2.2 | mocki GitLab (OAuth + `/api/v4/user` + REST), Confluence, Service Desk, OpenSearch (`mocks/systems/`, `make mocks-test` 165 testów); usługi w compose bez profilu | zrobione, mocki działają na stosie |
| M2.3 | Keycloak: redirect BFF `http://localhost:18740/api/v1/auth/callback` (realm i działający Keycloak przez `scripts/kc-sync-bff-client.sh`, bez restartu); Redis: użytkownik `portal-bff`; OpenBao: `secret/mcp/oauth-clients/gitlab` + odczyt w polityce brokera; broker z HEAD (8e7f037) z GitLabem i Confluence; profil `m2` z proxy, web, BFF i czterema serwerami ws-04 (`make images-m2`, `make up-m2`) | zrobione; BFF działa w kontenerze |
| M2.4 | `make m2`: 85 sprawdzeń (mocki, OAuth, broker, Redis ACL, Keycloak, portal przez proxy, klient MCP), 2 SKIP | zrobione |

Sprawdzone: `make m1` przechodzi po zmianach (jedno nieudane F1/A9 zaraz po poprzednim `make m1`, gdy broker dopiero
wstawał po A15; powtórka zielona). Przepływ "Połącz przez OAuth" z prawdziwym brokerem (`oauth/start` -> mock ->
`oauth/complete`): connected jako alice, `identity_mismatch` (konto bob), `flow_invalid` (zły state), odmowa
(`access_denied` w callbacku), rozłączenie z odwołaniem tokenu.

Decyzje i odstępstwa:

- Adres `http://localhost:18740` bez TLS: `localhost` jest zaufanym originem dla ciasteczek `__Host-`/`Secure` w
  Chrome i Firefoksie; Safari ich nie przyjmuje po http.
- Mocki są w Pythonie (stdlib), nie w WireMocku: OAuth potrzebuje stanu (kody, rotacja refresh tokenów, PKCE).
  Mock Jiry zostaje na WireMocku.
- Mock Confluence zwraca dane każdemu znanemu PAT (bez 403); ograniczenia przestrzeni wynikają z polityki
  gateway'a. Mock GitLaba odmawia dostępu do grup spoza tabeli `GROUP_ACCESS` jako 404.
- Mock OpenSearch nie sprawdza podpisu JWT (tylko `iss`, `aud`, `exp`); prawdziwy OpenSearch z JWT backendem jest poza M2.
- Poziomy `acr` (step-up) nie są w realmie: od CC-090 zapis PAT nie wymaga MFA, a rejestracja Authenticatora to M3.
- Redis `portal-bff` ma szerszy zestaw komend (`@keyspace`, `@scripting`) niż inne komponenty, na żądanie ws-07, ale tylko
  dla `~portal:*` i bez pub/sub; BFF trzyma sesje w pamięci, więc użytkownik jest na zapas.
- `make up` po tej zmianie odtworzy Keycloaka (nowa zmienna `PROXY_PORT`) i Redisa (nowy użytkownik): robi to
  tylko ten, kto go uruchomi; `make up-m2` używa `--no-recreate`.

### Runda poprawek M2 (przegląd Opus, 2026-10-28)

Stan po rundzie: `make m1` trzy razy pod rząd zielone, `make m2` 85 PASS / 0 FAIL / 2 SKIP, `make proxy-test` 200,
`make mocks-test` 174. Stos: gateway `m2-8dc1d7a`, broker 8eac9d8, BFF i web z HEAD mcp-portal (1c2a769), serwery ws-04
z HEAD; BFF działa w kontenerze i loguje przez Keycloaka za proxy (opis: [docs/m2.md](docs/m2.md)).

| Znalezisko | Status |
| --- | --- |
| H1 ACL Redis `portal-bff` | poprawione: jawna lista komend w `redis/portal-bff.rules` (jedno źródło dla `start.sh` i `redis-sync-acl.sh`), tylko `~portal:*`; zastosowane na działającym Redisie; `make m2` sprawdza NOPERM dla FLUSHALL/FLUSHDB/SWAPDB/KEYS/SCAN/RANDOMKEY/DBSIZE/SCRIPT/FUNCTION/MIGRATE/CONFIG, `gw:*`, `broker:*`. Dodatkowo (koordynator): broker ma `+eval +evalsha` (`redis/broker.rules`), skrypt na kluczu spoza `~broker:*` jest odrzucany, SCRIPT i FUNCTION nie |
| M1 błędy proxy bez nagłówków | poprawione: odmowa ścieżki nie odpala się drugi raz przy przekierowaniu do `/_err/` (mapy `volatile`), `error_page` dla 494 (431), 501 (400), 408, 411; wybór strony błędu także po zdekodowanym URI (`/%73/x`); `proxy-test` sprawdza `Content-Type`, `nosniff`, CSP i jednokrotność nagłówka dla każdego przypadku |
| M2 drugi origin gateway'a | poprawione: `MCP_PUBLIC_URL` = proxy, `resource` w RFC 9728 sprawdzany w `make m2`; port 18000 zostaje wyłącznie dla `make m1` (harness mcp-e2e łączy się wprost), opisane w `docs/porty.md`. Obraz `m2-8dc1d7a`, alias `mcp-gateway.internal:8443` (C5), certyfikat `mcp-portal-bff`, cztery serwery ws-04 |
| M3 Host i X-Forwarded-* | poprawione: stały `Host`/`X-Forwarded-Host`/`X-Forwarded-Port`, czyszczenie Prefix/Ssl/Scheme/X-Original-URL/X-Rewrite-URL; `PORTAL_TRUSTED_PROXIES` = adres proxy (entrypoint BFF rozwiązuje `proxy.internal`; stałego IP nie dałem, bo zmiana IPAM wymusiłaby odtworzenie sieci współdzielonego stosu) |
| M4 mock GitLaba i `scope` | poprawione: scope egzekwowany (403 `insufficient_scope`), brak lub nieznany scope w authorize -> `invalid_scope`; testy jednostkowe i `make m2` (zapis przez `read_api` kończy się błędem). Decyzja dla ws-04/ws-06: broker prosi o `read_api`, więc `gitlab__comment_merge_request` na połączeniu OAuth nie zadziała jak w prawdziwym GitLabie |
| M5 GitLab przez OAuth z zakresem `api` | decyzja człowieka 2026-10-28: `BROKER_SYSTEMS_GITLAB_OAUTHSCOPES: api` w compose (broker zrestartowany przez `make up-m2`), mock bez zmian (zakres `api` był już dozwolony); `make m2`: link z `gitlab__comment_merge_request`, wywołanie przez `/v/<slug>/mcp` kończy się sukcesem, notatka w MR widoczna w dzienniku mocka (201), link usuwany. Zastępuje decyzję z wiersza M4 o `read_api`; `make m1` i `make m2` bez FAIL (88 PASS, 2 SKIP) |
| M5 determinizm `make m1` | poprawione: healthchecki gateway'a, brokera, `mcp-jira` i serwerów ws-04; `m1-compose.py` czeka na `healthy` po restarcie brokera (A15) oraz na działające wywołanie gateway -> broker (gateway odmawia przez kilka sekund po powrocie brokera); preflight wymaga `healthy` |
| M6 `up-m2` zależny od historii | poprawione: bez `--no-recreate`, Compose odtwarza zmienione usługi; `make m2-sync` (Keycloak, Redis ACL, restart proxy) wołany z `up-m2`. Skutek uboczny: pierwszy `up-m2` po tej zmianie odtworzył Keycloaka i Redisa |
| L1 normalizacja ścieżki | częściowo: odrzucane `;`, `%3b` i każde `%` na trasach gateway'a. Zostaje overlong UTF-8 w segmentach SPA/BFF (nieszkodliwe: gateway ma StrictHttpFirewall) |
| L2, L3, L4 mock GitLaba | poprawione: ponowne użycie kodu unieważnia tokeny, walidacja formatu PKCE, nie-ASCII challenge -> 400 `invalid_request`/`invalid_grant`, dwie metody uwierzytelnienia i powtórzone parametry -> `invalid_request` |
| L5 OpenBao | częściowo: polityka brokera na dokładną ścieżkę `oauth-clients/gitlab`, sekret klienta usunięty z env brokera (czyta z OpenBao) |
| L6 redirecty Keycloaka | poprawione: dokładne `.../api/v1/auth/callback` (proxy i `:18090`), post-logout dokładne `/`; `kc-sync-bff-client.sh` czyta listę z realmu |
| L7 sekrety w argv | poprawione w `kc-sync-bff-client.sh` i `redis-sync-acl.sh` (pliki `/dev/fd`, stdin); `make pat` i inne skrypty już używały środowiska |
| L8 kody w access logu | poprawione: log ze ścieżką bez query |
| L9 limity połączeń | nie zrobione (dev, jeden użytkownik): `worker_connections` 256, SSE do 1 h |
| L10 `make m2` zmienia stan brokera | nie zrobione: przebieg używa alice i sprząta połączenie GitLab oraz link; wymaga osobnego użytkownika testowego w realmie |
| L11 prefiks well-known | poprawione: `location =` i `^~ .../` zamiast prefiksu bez granicy segmentu |

Zmiany poza listą: realm i skrypty nie używają już redirectu `/login/oauth2/code/*`; `identity_mismatch` w `make m2` zastąpiony
sprawdzeniem zgodnym z kontraktami 2.2.3 (brak porównania tożsamości).

SKIP w `make m2` (2): interfejs web w przeglądarce (brak sterownika przeglądarki w środowisku; te same wywołania API są
w przebiegu) i Claude Code z linkiem (wymaga człowieka). Zero SKIP blokuje: przeglądarka (Playwright/e2e ws-08 na
`http://localhost:18740`) oraz człowiek z Claude Code.

Do zrobienia: poświadczenie `service-account` service desku i `token-exchange` OpenSearch w brokerze (kroki spoza

### Serwery MCP ze starterem 3.0.0-alpha-11 (2026-10-28)

Obrazy przebudowane z HEAD przez `scripts/build-images-m2.sh` (confluence, gitlab, servicedesk, opensearch) i
`ONLY=jira scripts/build-images.sh`: jira `m1-7ec295e`, confluence `m2-ae9fe3a`, gitlab `m2-9ef55d3`, servicedesk
`m2-63e56cb`, opensearch `m2-7619dd4`; tagi w `.env`, wdrozone przez `make up-m2` (odtworzone tylko te piec uslug).
Starter alpha-10+ odmawia profilu dev w kontenerze: zadna z pieciu uslug nie ma `SPRING_PROFILES_ACTIVE` (maja
`MCP_<SYSTEM>_ALLOW_PLAIN_HTTP`); profil dev ma tylko `portal-bff`, ktory nie uzywa startera. Wyniki: `make m1` 7 PASS,
`make m2` 88 passed, 0 failed, 2 skipped, `mcp-e2e/run-m2.sh` wszystkie PASS. Zmiany ksztaltu odpowiedzi (opensearch
`data` jako lista, budzet wyniku; gitlab `hiddenCharactersRemoved`; servicedesk escapowanie notatki) nie wymagaly zmian
asercji w `make m2` (sprawdza `noteId`, status 201 po stronie mocka, `credential_required`).
`make m2`); BFF potrzebuje osobnego ustawienia backchannel OIDC zamiast `idp-relay` (propozycja poniżej).

### Adres publiczny na https (2026-10-28)

Powód: Safari nie zapisuje ciasteczek `__Host-`/`Secure` z `http://localhost` ("Sesja wygasla" po logowaniu do portalu).

- Proxy: drugi listener TLS, `https://localhost:18745` (`PROXY_TLS_PORT`; wolny w zakresie 18740-18759, 18743/18744 to mocki). Te same trasy i reguly
  (`proxy/routes.conf` wspolny dla obu listenerow), `X-Forwarded-Proto: https` i origin `localhost:18745` na tym listenerze. http `:18740` zostaje bez
  przekierowania (make m1, health, narzedzia bez TLS); bez HSTS.
- Certyfikat: osobne CA "dev public" (`certs/public-ca/`, `scripts/gen-public-cert.sh`, wywolywany przez `make certs`) z name constraints
  localhost/127.0.0.1/::1; wewnetrzne CA od mTLS nie ma ograniczen nazw, wiec nie nadaje sie do zaufania w pęku kluczy. Klucz CA nie jest montowany ani w repo (`/certs/` w .gitignore).
- `MCP_PUBLIC_URL`, `PORTAL_PUBLIC_BASE_URL`, `MCP_PORTAL_URL`, `MCP_CONNECT_URL` serwerow i redirect mocka GitLaba: https. Redirect URI i post-logout klienta
  `mcp-portal-bff` (https) w `realm-corp.json`, zastosowane do dzialajacego Keycloaka przez `scripts/kc-sync-bff-client.sh` (bez resetu).
- BFF w compose: `PORTAL_CLIENT_CONFIGS_CLAUDE_CODE_CLIENT_ID=claude-code`, `PORTAL_CLIENT_CONFIGS_CLAUDE_CODE_CALLBACK_PORT=33418` (usluga `portal-bff`).
  Realm ma publiczny klient `claude-code` z redirect `http://localhost:33418/callback`.
- `make trust-ca` drukuje polecenia dla czlowieka (`security add-trusted-cert ...`, `NODE_EXTRA_CA_CERTS`); niczego nie uruchamia.
- `scripts/m2.py` idzie przez https z CA "dev public". `make proxy-test`: 212 PASS. `make m2`: 88 PASS, 2 SKIP. `make m1`: F1, A1, A2, A3, A9, A9b, A15 PASS.
- Keycloak zostal wtedy na http (zalatwione w sekcji "Keycloak na https" ponizej).
- Definicja kontenera Keycloaka zmienila sie (env `PROXY_TLS_PORT`), wiec najblizsze `make up-m2` go odtworzy (realm importowany od nowa z nowymi URI); w tym przebiegu
  przeladowalem tylko proxy, gateway, broker, BFF, mock GitLaba i serwery MCP (`up -d --no-deps`).
- mcp-e2e (`run-m2.sh`): BLOCKED, bo harness jest na `http://localhost:18740` (`e2e/portal.py` PROXY/HOST, `e2e/m2.py` MCP_BASE, `raw_request` na http.client,
  gniazdo surowe w m2.py, `h.McpClient(base=...)`). Dla ws-10: zmienic origin na `https://localhost:18745`, kontekst TLS z `certs/public-ca/ca.crt`
  (HTTPSConnection, ssl.wrap_socket dla surowych zadan), a regex "obcych portow" w m2.py:217 na `:18745`.

### Integracja rundy domykajacej M2 (2026-10-28)

Obrazy (`.env`): gateway `mcp-gateway:m2-d65088f` (wczesniej na `m2-e4608ea`, potem poprawka bezpieczenstwa), broker `mcp-credential-broker:m2-bcfec7a`,
BFF `mcp-portal-bff:m2-93f0e9f` (mcp-portal 93f0e9f), web `mcp-portal-web:m2-2134a8e` (mcp-portal 2134a8e). Przeladowane `up -d --no-deps`; Keycloak nie ruszany.
Na `m2-d65088f`: `make proxy-test` 212 PASS; `make m2` 88 PASS, 0 FAIL, 2 SKIP; `make m1` F1, A1, A2, A3, A9, A9b, A15 PASS;
`run-m2.sh` (mcp-e2e 26da660, https): wszystkie PASS poza A5.
A5 FAIL: gateway odpowiada `not_found_or_denied` (a nie `tool_disabled`) na narzedzie spoza linku i nieistniejace; harness e2e oczekuje `tool_disabled`
(sprawa ws-10 albo ws-05, jesli `tool_disabled` ma zostac w kontrakcie). `scripts/m2.py` przyjmuje juz `not_found_or_denied`.
Komenda z portalu (`clientConfigs.claudeCode`): `claude mcp add --transport http --client-id claude-code --callback-port 33418 <slug> https://localhost:18745/v/<slug>/mcp`.

### Keycloak na https (M3, 2026-10-28)

Powod: Keycloak na `http://localhost:18080` ustawial `AUTH_SESSION_ID` i `KC_RESTART` z `Secure; SameSite=None`, a Safari nie zapisuje
takich ciasteczek z http, wiec formularz logowania nie dzialal.

Wybrana opcja (a): Keycloak na wlasnym porcie https, `https://localhost:18746` (`KEYCLOAK_TLS_PORT`), certyfikat z CA "dev public"
(`certs/keycloak`, te same name constraints; `make certs` dopisuje go bez ponownego generowania CA, ktore czlowiek juz zaufal).
Opcja (b), Keycloak za proxy pod prefiksem sciezki na `:18745`, wymagalaby nowej trasy proxy poza zestawem z C3 (`/`, `/api`, `/s/`, `/v/`,
`/.well-known/oauth-protected-resource*`), czyli zmiany kontraktu, i przepisania adresow Keycloaka (prefiks sciezki, `KC_HTTP_RELATIVE_PATH`).
Dodatkowo (b) daloby `iss` z prefiksem na tym samym origin co portal, a ciasteczka `__Host-` portalu i Keycloaka dzielilyby wtedy origin.

Nowy `iss`: `https://localhost:18746/realms/corp` (`KC_HOSTNAME=https://localhost:18746`, `KC_HOSTNAME_BACKCHANNEL_DYNAMIC=true`).
Kontenery nadal ida do Keycloaka po http aliasu sieci (`http://keycloak:8080`: JWKS, token endpoint, token exchange), wiec zaden kontener
Java nie potrzebuje CA w truststore, a `iss` jest tylko porownywany. Port http `18080` zostaje na hoscie dla skryptow (`make token`, admin API w
`kc-sync-bff-client.sh`); `mcpdev.py` domyslnie uzywa https z `certs/public-ca/ca.crt`.

Konfiguracja konsumentow (tylko zmienne srodowiskowe, kodu nie ruszano):
- gateway: `MCP_OIDC_ISSUER` (C1 `iss` oraz `authorization_servers` w metadanych RFC 9728, sprawdzone: `["https://localhost:18746/realms/corp"]`);
  JWKS i token URI bez zmian (http);
- broker: `MCP_OIDC_ISSUER`, jw.;
- BFF: `PORTAL_OIDC_ISSUER_URI`. BFF robi discovery pod adresem issuera, wiec `idp-relay` (ten sam namespace sieciowy) slucha teraz
  na `127.0.0.1:18746` po https (certyfikat `certs/keycloak`, port generowany z `KEYCLOAK_TLS_PORT`) i przekazuje do `https://keycloak:8443`
  z tym samym `Host`; Java BFF ufa CA "dev public" przez `JAVA_TOOL_OPTIONS` z `certs/public-ca/truststore.p12`, bo klient OIDC nie uzywa
  `PORTAL_TLS_TRUSTSTORE` (to tylko mTLS). Blokady w kodzie BFF nie ma;
- serwery MCP nie sprawdzaja issuera Keycloaka (weryfikuja tokeny gateway'a, `iss=https://mcp-gateway.internal`), bez zmian;
- mock OpenSearch (ten repozytorium) mial na sztywno `iss`; teraz `MOCK_OPENSEARCH_ISSUER` z compose.

Keycloak byl odtworzony (zmiana srodowiska kontenera), realm importowany od nowa. Zeby `sub` uzytkownikow (a z nim polaczenia PAT/OAuth w brokerze)
przetrwal, `keycloak/realm-corp.json` ma teraz stale `id` czterech uzytkownikow (wartosci `sub` sprzed zmiany, sprawdzone po odtworzeniu).
Sesje SSO Keycloaka i sesje portalu zostaly zerwane; tokeny Claude Code sprzed zmiany maja stary `iss` i gateway je odrzuci.

Testy: `make proxy-test` 212/0; `make m2` 88 passed, 0 failed, 2 skipped; `make m1` F1, A1, A2, A3, A9, A9b, A15 PASS; `make mocks-test` 174 OK.
`make smoke` wymaga profilu infra ze stubami (nie biegnie przy `up-full`), nie uruchamiany.

## M3 (2026-10-28): infrastruktura dla Authenticatora, Cedar i logow aktywnosci

Opis: [docs/m3.md](docs/m3.md). Kontrakty 3.0.1. Kamienie:

| Kamien | Zakres | Stan | Commit |
| --- | --- | --- | --- |
| M3.1 | OpenBao (klucz Transit `mcp-authenticators`, polityka brokera), Redis (`redis/gateway.rules`), Keycloak (przeplyw `browser-corp`, `acr` `password`=1 / `mfa`=2, OTP dla alice/bob/carol, ziarna z `init-secrets.sh`), serwer paczek `policies` (:18747), mock Jiry `DELETE issue`, `scripts/totp.py` | zrobione | 11eaf3c |
| M3.2 | `make m3` (37 kontroli infrastruktury + scenariusz Authenticatora z SKIP), `make otp`, `make reset-authenticator`, grupa `mcp-jira-destructive` dla alice, docs | zrobione | patrz git log |
| M3.3 | integracja obrazow M3 (gateway m3-7315893, broker m3-b4dd446, BFF m3-44dc221, web m3-de48d2f), bez `idp-relay`, Redis AOF + noeviction, wolumen stanu gateway'a, `MCP_POLICY_*` | zrobione; `make m2` 80/8 przez polityke klienta (nizej), mcp-jira bez `delete_issue` | patrz git log |

Runda 2 (gateway m3-7315893, paczka 20261028210510-bec5815 z mcp-portal-bff): proxy-test 212, m1 7 PASS, m2 88 PASS / 0 FAIL / 2 SKIP,
m3 54 PASS / 0 FAIL / 2 SKIP, run-m2.sh wszystkie PASS; blokada polityki klienta z rundy 1 usunieta. Ponizej wyniki rundy 1.

Integracja M3.3 (2026-10-28), wyniki rundy 1: `make proxy-test` 212; `make m1` 7 PASS; `make m3` 54 PASS / 0 FAIL / 2 SKIP (wykonanie
`delete_issue` i powtorzenie kodu: `mcp-jira` bez narzedzia, kod przyjety, serwer odpowiada Invalid params); `make m2` 80 PASS /
8 FAIL / 2 SKIP; `mcp-e2e/run-m2.sh` pada na F3, A5b, A7, A8, A13 i scenariuszach (ten sam powod).
Przyczyna FAIL w m2 i run-m2: paczka polityk ws-09 (rewizja 20261028201639-742561e) ma `platform-client-not-allowed`
(`forbid ... unless ["claude-code"].contains(context.client)`), a operacje C5 BFF-a (katalog, tworzenie linku) niosa `client` =
`mcp-portal-bff`: gateway odpowiada `not_granted` w `virtual_server_change`, portal 403 `access_denied`, katalog pokazuje
wszystkie narzedzia jako `denied`. Do rozstrzygniecia miedzy ws-05 (nie oceniac klienta dla operacji administracyjnych)
a ws-09 (dopisac `mcp-portal-bff` albo ograniczyc regule do `Action::"call"`).

Runda 3 (serwery beta-3: jira m3-198c8ca, confluence m3-759efc6, gitlab m3-c234e67, servicedesk m3-549cf02, opensearch m3-dae2b19; paczka
20261028211441-689e217; rejestr `required` z bootstrapem manifestow): proxy-test 212, m1 7 PASS, m2 88/0/2 SKIP, mocks-test 177, m3 61 PASS / 0 FAIL /
0 SKIP (w tym DELETE na mocku Jiry po kodzie i odrzucenie powtorzonego kodu, tools/list przez gateway dla pieciu serwerow), run-m2.sh wszystkie PASS.
Dopisane: `policies` na https (:8443), `server-state-init`, mock Confluence `space/{key}`, `gen-certs.sh` dopisuje nowe tozsamosci bez ruszania CA.

Przeglad Opus dev M3 (0 CRITICAL/HIGH, 3 MEDIUM, 11 LOW) i rotacja, poprawione w jednym kamieniu:

| Znalezisko | Co zrobione |
| --- | --- |
| M-1 `make users` drukowal ziarna OTP | filtr tylko `KC_USER_*_PASSWORD`; test w `make m3`; ziarno tylko przez `make otp URI=1` |
| M-2 klucz dev pod id `policies-main-dev` | serwery na starterze beta-4 z profilem `devstack` i zgodami, id z powrotem `test-policies-main-dev`; test w `make m3`, ze klucze w compose pochodza z `mcp-policies/keys/dev` |
| M-3 step-up bez OTP przez samodzielna konfiguracje | `CONFIGURE_TOTP` wylaczone w realmie: mallory z `acr_values=mfa` dostaje 400 bez tokenu (test negatywny sprawdza odmowe); `bruteForceProtected` wlaczone (prog 30) |
| L-1, L-2 Redis | reguly wczytywane przed zapisem ACL (blad zamiast uzytkownika bez komend), kontener Redisa odtworzony z montowaniem `gateway.rules` i wolumenem `redis-data` |
| L-3 Transit bez derywacji | opisane jako decyzja w `docs/m3.md`, bez zmiany |
| L-5, L-6 klucze | `gen-public-cert.sh` ustawia 600; gateway, BFF i serwery montuja tylko `keystore.p12`; stuby healthchecku nadal maja certyfikat BFF (osobna tozsamosc: nie zrobione) |
| L-7 brute force | patrz M-3 |
| L-8 bootstrap rejestru | `make registry-refresh` zamiast recznego SQL; brak straznika w gatewayu zgloszony do ws-05 |
| L-9, L-10, L-11 docs | ostrzezenie o ziarnach i kodach, sciezka od zera, poprawione sprzeczne zdanie, STATUS, `porty.md` (listener https), README |
| L-4 gateway po http do paczek | bez zmian (ruch w sieci `core`, podpis chroni archiwum); do decyzji |
| Rotacja | ziarna OTP i haslo admina Keycloaka wygenerowane na nowo, Keycloak odtworzony (`sub` bez zmian) |

Poprawki po przegladzie 6e0e83c (przygotowane, NIE zastosowane na dzialajacym stosie do zgody koordynatora): N-2 usunieta flaga
`INTERNAL_JWT_ALLOW_TEST_KEYS` z piatki serwerow (gateway podpisuje JWT kluczami efemerycznymi), N-3 komentarz o metryce `mcp_server_devstack_active`,
N-4 i N-5 testy w `make m3` na efektywnej konfiguracji compose (`compose.sh config`) i na wartosciach ziaren, `make admin-password` zamiast hasla admina w
`make users`, N-6 `registry-refresh.sh` z liczba serwerow z compose i kodem 1 przy niekompletnym wyniku, N-7 polecenie rotacji i uwaga o Linuksie w `docs/m3.md`.
Zastosowanie: `scripts/compose.sh --profile full --profile m2 up -d --no-deps --wait mcp-jira mcp-confluence mcp-gitlab mcp-servicedesk mcp-opensearch`
(odtworzy piatke serwerow bez zmiany stanu rejestru; potem sprawdzic `tools/list`, `make m3`).

Broker a systemy `opensearch` i `servicedesk` (diagnoza `search_logs` -> `internal_error`): broker stosu zna tylko `jira` (z `application.yml`), `confluence` i `gitlab`
(`BROKER_SYSTEMS_*` w compose). Wywolanie narzedzia opensearch/servicedesk konczy sie 400 "system is not configured" u brokera, a gateway mapuje to na `internal_error`.
Konfiguracji w compose nie dopisuje: broker ma tylko adaptery `atlassian-dc` i `gitlab` (whoami), a gateway wymaga dla `opensearch` typu `token-exchange`, dla `servicedesk`
`service-account`; oba typy sa w brokerze przeniesione poza M3 (STATUS brokera). Zadanie dla ws-06: adapter/typy `token-exchange` (OpenSearch) i `service-account` (service desk, konto
serwisowe z OpenBao). Uwaga dla ws-04/ws-05: manifest serwera opensearch w compose ma `MCP_ACCEPTED_CREDENTIAL_TYPES=user-secret`, a gateway `accepted-types: [token-exchange]`.
`make m3` ma test `broker: the system <x> is configured` dla kazdego serwera z compose (katalog portalu ma wpis `credential`); dla `opensearch` i `servicedesk` jest SKIP ze znana
przyczyna (`BROKER_GAPS` w `scripts/m3.py`), kazdy inny system bez wpisu to FAIL.

Zastosowane 2026-10-29 (`make m3-switch`; po zastosowaniu: proxy-test 212, m1 7 PASS, m2 88/0/2 SKIP, m3 70 PASS / 0 FAIL / 2 SKIP znanej luki brokera): gateway `m3-1f7f543` z profilem devstack (N-1 zamkniete po stronie gatewaya); `make m3-switch` odtwarza broker, serwery i gateway;
test `gateway: the running container logs event=devstack_profile` w `make m3` jest SKIP do zastosowania. Po zastosowaniu zmienne `MCP_POLICY_ALLOW_TEST_KEYS`,
`MCP_REGISTRY_BOOTSTRAP`, `MCP_ALLOW_PLAIN_HTTP` obowiazuja za podwojna zgoda (wpis N-1 nizej jest wtedy zalatwiony).

Otwarte:
- Odrzucanie kluczy dev ws-09 po odcisku SPKI przez odbiorcow (starter, gateway): lista publikowana przez ws-09/ws-00, nie w beta-4. Dzis ochrone daje profil
  `devstack` z podwojna zgoda i nazwa `test-*`.
- Straznik profilu w gatewayu (N-1, MEDIUM, zlecone ws-05): `test-*`, `MCP_REGISTRY_BOOTSTRAP` i http do paczek pod jednym profilem z podwojna zgoda. Po nowym obrazie
  gatewaya ustawie w compose nowe zmienne (dzis `MCP_POLICY_ALLOW_TEST_KEYS`, `MCP_REGISTRY_BOOTSTRAP`, `MCP_ALLOW_PLAIN_HTTP` sa ustawione bez zgody).
- L-6: stuby healthchecku (`broker-mock`, `gateway-admin-mock`, profil `infra`) nadal uzywaja certyfikatu `mcp-portal-bff` jako klienta healthchecku; osobna
  tozsamosc wymaga zmiany sprawdzania w stubach z `mcp-contracts` (CN spoza listy daje 403), nie zrobione.
- Gateway pobiera paczki po http :8080 (ruch w sieci `core`); serwery po https :8443.

Stan biezacy (serwery beta-4: jira m3-d2f296a, confluence m3-3c7e396, gitlab m3-fedd052, servicedesk m3-ae76954, opensearch m3-3cd6bfa): proxy-test 212,
m1 7 PASS, m2 88/0/2 SKIP, m3 65 PASS / 0 FAIL / 0 SKIP, run-m2.sh wszystkie PASS. `make m3` i `mcp-e2e/e2e/m3.py` uzywaja tego samego Authenticatora alice:
nie uruchamiaj ich rownolegle.

Wartosci: `acr` MFA = `mfa` (zwykle logowanie `password`); `PORTAL_STEP_UP_ACR_VALUES=mfa` jest ustawione w compose.
Zmiany na dzialajacym stosie bez `down`: ponowny przebieg `openbao-init`, `redis-sync-acl.sh`, odtworzony Keycloak
(sesje zerwane, `sub` bez zmian), `portal-bff` z `PORTAL_STEP_UP_ACR_VALUES`, restart `jira-mock`, nowa usluga `policies`,
alice dodana do `mcp-jira-destructive` przez kcadm. Redis nie byl odtwarzany (compose ma nowy wolumen `gateway.rules`,
nastepne `make up` go odtworzy). (`idp-relay` usuniety w M3.3.)
Wyniki po zmianach: `make proxy-test` 212, `make m2` 88 PASS / 0 FAIL / 2 SKIP, `make m1` 7 PASS, `make m3` 37 PASS / 8 SKIP.
Nie zweryfikowane: scenariusz M3 na prawdziwych komponentach, zachowanie step-up Keycloaka z istniejaca sesja SSO w przegladarce.

## M4 (2026-10-29): migracja do mcp-contracts 4.0.0, przygotowanie (stos M3 nietknięty)

Opis: [docs/m4.md](docs/m4.md). Warstwa M4 compose (`docker-compose.m4.yml`, `m4.env`) działa tylko przy `M4_ACTIVE=1`, które ustawia `make m4-switch`; bez niego efektywna konfiguracja jest M3. W tej fazie nic nie zrestartowano na działającym stosie; wszystko jest w repo, a aktywacja to `make m4-switch`
na polecenie koordynatora. Efektywny `compose config` różni się od poprzedniego tylko zmienną `SERVICEDESK_API_KEY` jednorazowego `openbao-init`.

| Kamień | Zakres | Stan |
| --- | --- | --- |
| M4.1 | certyfikaty CI per serwer (5), `certs/legacy/mcp-registry-ci` do testu 403, klucze 600; identyfikatory kluczy polityk w `.env` (domyślnie `test-`, `dev-policies-main`/`-overrides` przy przełączeniu); konto serwisowe Service Desku w OpenBao + polityka brokera; token exchange do OpenSearch sprawdzony na mocku | zrobione; `make m4` 22 PASS, 14 SKIP ("przed przelaczeniem" lub brak wykonawcy), `make openbao-test` 8 PASS |
| M4.2 | `make offboard`, `make registry-register` (certyfikat serwera, manifest z `mcp-app/target/mcp-manifest.json`), `scripts/m4.py` (część po przełączeniu), `make m4-test` 6 testów na fałszywych Keycloak, brokerze i gateway'u z mTLS | zrobione; część po przełączeniu nieuruchomiona na prawdziwych komponentach |
| M4.3 | `make m4-switch` (kolejność: OpenBao, Redis ACL, broker, serwery, kasowanie wierszy bootstrap, gateway, rejestracja CI, BFF/web, sygnał do ws-09 i oczekiwanie na paczkę 4.0.0, testy); `CHECK=1` sprawdza warunki wstępne | skrypt gotowy, `--check` zgłasza brak obrazów M4; samo przełączenie czeka na obrazy i polecenie |

Weryfikacja: `compose config -q` (profile infra, full, m2), `make mocks-test` 177, `make m4-test` 6, `make proxy-test` 211 z 212 (nieudany: "wewnętrzne CA nie weryfikuje certyfikatu publicznego";
curl z SecureTransport na macOS bierze zaufane CA z pęku kluczy, gdzie człowiek zaufał CA "dev public"; test nie jest związany ze zmianami M4).

IGA: adapter `mock` jest w procesie BFF (compose ma `PORTAL_IGA_MOCK_FILE`), osobnej usługi nie dodano.
Dotychczasowe `make registry-refresh` zostaje jako tor dev (bootstrap); tor produkcyjny to `make registry-register`.

Czekam na (do przełączenia):

- ws-05: obraz gateway'a 4.0.0 (`MCP_GATEWAY_IMAGE` `m4-...`) i jego zmienne: konfiguracja limitów (użytkownik, klient, narzędzie), TTL odczytu `tools/list` (<= 60 s),
  `credentialType` per system, nazwa ustawienia trybu dev z `allowTestKeys`, potwierdzenie listenera C5 na `:8443` dla CN `mcp-offboarding` i `mcp-registry-ci-<serverId>`;
- ws-06: obraz brokera 4.0.0; zmienne systemów `opensearch` (`token-exchange`, audience i zakres) i `servicedesk` (`service-account`), odczyt `secret/data/mcp/service-accounts/<system>`
  (potwierdzenie pól `key` i `scheme`), domyślny limit rejestracji, `unused_cleanup`;
- ws-02/03/04: obrazy pięciu serwerów 4.0.0 (`MCP_JIRA_IMAGE`, `MCP_CONFLUENCE_IMAGE`, `MCP_GITLAB_IMAGE`, `MCP_SERVICEDESK_IMAGE`, `MCP_OPENSEARCH_IMAGE`) i `mcp-app/target/mcp-manifest.json`
  każdego (z `environment: dev` w profilu dev; OpenSearch z `token-exchange` w `acceptedTypes`), zmienne `MCP_ACCEPTED_CREDENTIAL_TYPES` i dla Service Desku `service-account`;
- ws-07/08: obrazy BFF i web 4.0.0 (`MCP_PORTAL_BFF_IMAGE`, `MCP_PORTAL_WEB_IMAGE`) i ewentualne nowe zmienne `PORTAL_*`;
- ws-09: kanał paczek 4.0.0 (`out/repository-m4/`, rewizja 20261029162141-a6152ef, klucze `dev-policies-main` i `dev-policies-overrides` w `mcp-policies/keys/dev`) i publikacja
  `.ci/switch-stack.sh --confirm` na sygnał z `make m4-switch`;
- ws-10: nic do przełączenia; `make m4` i scenariusze e2e po przełączeniu.

Propozycje CC (nie zmieniano kontraktów):

- Układ sekretu konta serwisowego w OpenBao (`value`, `username`+`password`, `expires_at`) i klienta wymiany (`oauth-clients/<system>`, `client_secret`) nie jest w kontrakcie (to samo zgłasza ws-06 jako P2).
- Test `environment_mismatch` wymaga gateway'a w trybie produkcyjnym; stos dev go nie ma. Do rozważenia: wektor lub test e2e uruchamiający drugi gateway bez `devstack`.

## M4: stos przełączony (2026-10-29)

`make m4-switch` zastosowane na działającym stosie (bez down i bez usuwania wolumenów). Obrazy (z `m4.env`): gateway `m4-ac3cdb6`, broker `m4-27b8866`, BFF `m4-bd5a231`,
web `m4-58c0b7b`, jira `m4-7f3f951` (podmieniona po przełączeniu, poprawka wycieku przez `hasMore`), confluence `m4-bb3289e`, gitlab `m4-516853a`, servicedesk `m4-f248714`, opensearch `m4-1862fb4`.
Paczki polityk: kanał `repository-m4` rewizja 20261029192632-5625b13, klucze `dev-policies-*`.

- Kolejność poprawiona w skrypcie: serwery MCP M4 nie wstają bez paczki podpisanej kluczem `dev-` (padają po 30 s), więc sygnał do ws-09 i oczekiwanie na publikację są przed serwerami.
- Usunięte z compose `MCP_SERVICEDESK_CREDENTIAL_MODE` i `MCP_SERVICEDESK_ALLOWED_QUEUES`; kolejki nadaje tylko paczka polityk (CC-163).
- Sesje `gw:session:*` M3 skasowane przed gatewayem (było 0 kluczy); `sync-groups`: realm już zgodny (grupy dodane wcześniej).
- Rejestr: 5 serwerów zarejestrowanych certyfikatami CI (200), wiersze `bootstrap` skasowane. Oczekują na `approve` (pending_approval, `source=ci`, `change=new`): `jira` add_comment, transition_issue,
  delete_issue; `confluence` create_page; `gitlab` comment_merge_request; `servicedesk` add_note. Zatwierdzić może tylko `carol` (jedyna z `mcp-platform-admin`) w portalu.
- Mocki dostosowane do serwerów M4: Service Desk ma `GET /api/v1/queues/{queue}`; OpenSearch rozumie `multi_match` i agregację `terms` (aggregate_logs). `make mocks-test` 180.
- `_resolve/index` mocka OpenSearch: `logs-team-a,logs-team-a-*` z `ignore_unavailable=true` zwraca tylko istniejące indeksy; bez tego parametru brak indeksu o nazwie samego prefiksu daje 404 (jak OpenSearch).
- Token Keycloaka: nagłówek `typ: JWT` (nie `at+jwt`), broker go przyjmuje (łańcuch token exchange do OpenSearch działa przez gateway).
- Wyniki: `make m4` 39 PASS / 0 FAIL / 1 SKIP (`environment=dev` bez drugiego gateway'a), `make m1` zielone, `make m2` 85 PASS / 3 FAIL (zapis GitLab czeka na `approve`), `make m3` 57 PASS / 2 FAIL przed poprawką
  klucza w teście (podpis sprawdzany kluczem `test-`; poprawione) i brakującą listą narzędzi zapisu Jiry (pending), `make proxy-test` 211 z 212 (zaufanie CA w pęku kluczy macOS).

### E2E-2: grupy w portalu (2026-10-29)

BFF czyta grupy z ID tokenu i userinfo, a mapper `groups` klienta `mcp-portal-bff` ma `id.token.claim=false`, więc `/api/v1/me` miał puste grupy (carol nie była adminem).
`PORTAL_OIDC_USER_INFO_URI=http://keycloak:8080/realms/corp/protocol/openid-connect/userinfo` dodane w `docker-compose.m4.yml` (profil `dev` BFF dopuszcza http dla back-channel jak dla pozostałych URI);
realm bez zmian. Odtworzony tylko kontener `portal-bff`. Sprawdzone programowo: carol `/api/v1/me` ma `mcp-platform-admin`, `isAdmin=true`, `/api/v1/admin/registry` 200 z sześcioma narzędziami
`pending_approval` (nic nie zatwierdzono); alice `isAdmin=false`, `admin/registry` 403.

Zgłoszona regresja step-up po E2E-2 (30.10, 401 `step_up_required` mimo step-upu): hipoteza, że userinfo nadpisuje `acr`, **nie potwierdziła się**; nic nie zmieniono.
Logowanie `mcp-portal-bff` z `acr_values=mfa` i `max_age=0`: ID token ma `acr=mfa` i `auth_time`, userinfo zwraca tylko `email`, `email_verified`, `family_name`, `given_name`, `groups`, `name`,
`preferred_username`, `sub` (bez `acr` i `auth_time`), więc scalone claimy zachowują `mfa` z ID tokenu; claimy wspólne nie różnią się. Przez BFF na bobie (bez zarejestrowanego Authenticatora): `DELETE /api/v1/authenticator`
bez step-upu daje 401 `step_up_required`, po logowaniu ze step-upem 204. Log BFF alice: `step_up_required` o 12:26:26Z, a `authenticator_removed` (allow) o 12:28:09Z, potem to samo przy rejestracji
(deny 12:49:40Z, allow 12:50:42Z i 12:51:06Z): po step-upie operacje przechodzą, a 401 poprzedza go zgodnie z projektem.

### Testy nie ruszają Authenticatorów ludzi (2026-10-30)

`make m3` rejestrował, resetował i wyczerpywał kody na koncie alice (E2E-4 w mcp-e2e). Teraz scenariusz Authenticatora działa na użytkowniku jednorazowym (`mcpdev.TempUser`: losowa nazwa
`m4-<hex>`, hasło, poświadczenie OTP Keycloaka, grupy Jiry; PAT `mallory-pat` zapisany brokerowi), który po teście jest purge'owany (broker, gateway) i usuwany z realmu. `mcpdev.reset_authenticator` odmawia
alice, bob, carol i mallory bez `--force` (`make reset-authenticator USER=alice FORCE=1` to narzędzie człowieka; test jednostkowy sprawdza odmowę). `make m3` porównuje stan, `registration_id` i `registered_at` wierszy
alice, bob, carol w bazie brokera przed i po przebiegu (bez kolumn z sekretami) i zalicza to jako sprawdzenie. Wynik przebiegu po approve 6 narzędzi: `make m2` 88 PASS / 0 FAIL / 2 SKIP, `make m3` 73 PASS / 0 FAIL / 0 SKIP,
`make m4` 39 PASS / 0 FAIL / 1 SKIP; wiersze przed i po identyczne: alice `active`, `bdd72742368f1f47fe80f25e9298511c`, `2026-10-30 12:51:06.960096+00`; bob i carol `none`.
Uwaga: test błędnego OTP w `make m3` (`000000`) zwiększa licznik błędów Keycloaka alice; dwa przebiegi pod rząd w odstępie krótszym niż minuta mogą zablokować konto na 60 s (blokada czasowa, bez skutku dla Authenticatora platformy).

### L10 i test błędnego OTP (2026-10-30)

Dziennik mocków (`mocks/systems/common.py`, `describe_auth`) zapisuje znany PAT jako `pat:<konto>` (np. `pat:alice`), nie wartość; nieznane poświadczenia są oznaczane bez wartości; test `test_common.py`. Przeczytane pozostałe mocki: nic
innego nie loguje wartości. Zrestartowane mocki Confluence, Service Desku i OpenSearch (bezstanowe); mock GitLaba zostaje na starym kodzie do następnego restartu, bo trzyma w pamięci tokeny OAuth połączeń (restart wymagałby ponownego
"Połącz" w portalu). Test błędnego OTP w `make m3` idzie na `TempUser`, więc nie liczy błędów logowania alice. `make mocks-test` 182, `make m3` 73 PASS.

## Propozycje CC

Do przeniesienia do `CONTRACT_CHANGES.md` przez człowieka lub ws-00; kontraktów tu nie zmieniano.

- Brak zmian kontraktów. Prośba do ws-07 (nie CC): ustawienie backchannel OIDC w BFF (osobne adresy tokenu i JWKS przy `issuer-uri` do walidacji `iss`), żeby `idp-relay` nie był potrzebny.
- Zmienne `MCP_*` dla gateway'a, brokera i `mcp-jira` zgadzają się z compose; jedyne odstępstwo to
  `MCP_ALLOW_PLAIN_HTTP` gateway'a (token exchange do Keycloaka po http), które jest dev-only i już opisane w README gateway'a.

## Odstępstwa od briefu

- Hasła użytkowników, sekrety klientów, root token OpenBao, hasła Redis/PostgreSQL i keystore'ów są
  generowane do `.secrets/dev.env` przy pierwszym `make up` (brakujące klucze są dopisywane), a nie zapisane
  w repo. Realm używa placeholderów `${...}`. Wyjątek: PAT-y mocka Jiry (`alice-pat` itd.) są stałe, bo
  wymaga ich DoD i nie dają dostępu do niczego poza mockiem.
- Keycloak w trybie dev bez trwałego wolumenu: realm jest importowany przy każdym starcie kontenera.
- Dodatkowy klient `opensearch` i opcjonalne client scope'y `*-audience`: Keycloak 26.8 wymaga przy wymianie
  `scope=<audience>-audience` obok `audience`.
- `make token` nie używa ROPC: skrypt przechodzi prawdziwy authorization code + PKCE.
- Komponenty w kontenerach muszą używać JWKS `http://keycloak:8080/...` i oczekiwanego `iss`
  `https://localhost:18746/realms/corp` zamiast discovery z `issuer-uri` (`docs/porty.md`).
- Hasło keystore'ów stubów WireMock i hasło admina mocka Jiry są w argumentach procesu Javy w kontenerze
  (WireMock przyjmuje je tylko jako opcje); chronią testowe klucze i mock.

## Poza M1 (nie zrobione)

- (zrobione w M3) step-up flow z poziomami `acr`: `password` / `mfa`,
- OpenSearch z security pluginem, JWT backend, indeksy, DLS/FLS i dwa punkty DoD o OpenSearch,
- mocki Confluence, GitLaba, service desku oraz serwery OAuth (authorize/token/refresh) dla GitLaba i Jiry,
- (zrobione w M3) repozytorium paczek polityk (nginx `policies`, klucze dev ws-09),
- kolektor audytu ("SIEM"); w M1 `make audit` czyta logi kontenerów,
- mock Jiry: uprawnienia per użytkownik, endpointy zapisu, `project in (A, B)` w JQL (L10).

## Niesprawdzone

- Prawdziwe logowanie Claude Code w przeglądarce i wywołanie narzędzia z jego poziomu: wymaga człowieka
  (kroki w README). Sprawdzone elementy: PKCE, redirect, `aud`, metadane RFC 9728 i 8414, parametr `resource`.
- Klient Redis z Spring (Lettuce) przeciw ACL: komendy połączeniowe są w `@connection`, ale nie było
  komponentu do sprawdzenia.
- `make up` na innym systemie niż macOS (Docker Desktop).

## Otwarte pytania

- CC-035: rozstrzygnięte przez człowieka (nazwa TLS = konfiguracja komponentu); środowisko wystawia
  `MCP_BROKER_TLS_NAME` i `MCP_GATEWAY_INTERNAL_TLS_NAME`.
- Prefiksy kluczy Redis (`gw:`, `broker:`, `jti:<serverId>:`) są propozycją ws-01 do potwierdzenia przez
  ws-02, ws-05, ws-06.
- (rozstrzygniete w M3) `acr` MFA to `mfa`, bez MFA `password`.
- Stub `resolve` zwraca ten sam PAT (`jkowalski`) dla każdego użytkownika; demo M1 z tożsamością `alice`
  w Jirze wymaga prawdziwego brokera.
