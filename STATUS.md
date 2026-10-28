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
`make m2`); BFF potrzebuje osobnego ustawienia backchannel OIDC zamiast `idp-relay` (propozycja poniżej).

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
  `http://localhost:18080/realms/corp` zamiast discovery z `issuer-uri` (`docs/porty.md`).
- Hasło keystore'ów stubów WireMock i hasło admina mocka Jiry są w argumentach procesu Javy w kontenerze
  (WireMock przyjmuje je tylko jako opcje); chronią testowe klucze i mock.

## Poza M1 (nie zrobione)

- step-up flow z poziomami `acr` (hasło / hasło+OTP); teraz `acr` = `1` dla każdego logowania,
- OpenSearch z security pluginem, JWT backend, indeksy, DLS/FLS i dwa punkty DoD o OpenSearch,
- mocki Confluence, GitLaba, service desku oraz serwery OAuth (authorize/token/refresh) dla GitLaba i Jiry,
- repozytorium paczek polityk (MinIO/nginx) i testowy klucz Ed25519,
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
- Jakie wartości `acr` oznaczają MFA dla claimu `mfa` w C2 - do ustalenia przed step-up flow w realmie.
- Stub `resolve` zwraca ten sam PAT (`jkowalski`) dla każdego użytkownika; demo M1 z tożsamością `alice`
  w Jirze wymaga prawdziwego brokera.
