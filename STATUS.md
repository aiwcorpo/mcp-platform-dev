# Status ws-01 (mcp-platform-dev)

Cel sesji: kamień M1 (to, czego potrzebuje walking skeleton z `00-overview.md`). Brief ws-01 nie dzieli
pracy na kamienie, więc M1 = podzbiór produktów potrzebny do demo M1; reszta briefu jest niżej jako
"poza M1". Po przeglądzie M1 (2026-10-26) poprawione znaleziska H1-H2, M1-M6, L1-L9 (tabela niżej).

## M1: zrobione

Sprawdzone `make clean && make up && make smoke`: 92 sprawdzenia, wszystkie PASS; `make up` od zera ok. 20 s
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

- Prawdziwe logowanie Claude Code (`claude mcp add --client-id claude-code --callback-port 33418 ...`):
  bez gateway'a nie ma czego podłączyć. Sprawdzone elementy: PKCE, redirect, `aud`, metadane RFC 8414,
  tolerowanie parametru `resource`.
- Profil `full`: tylko `docker compose config`; nazwy zmiennych `MCP_*` dla komponentów to propozycja.
- `make audit` na prawdziwych logach komponentów (test tylko na zdarzeniach z wektorów kontraktu).
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
