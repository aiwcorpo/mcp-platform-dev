# Status ws-01 (mcp-platform-dev)

Cel sesji: kamień M1 (to, czego potrzebuje walking skeleton z `00-overview.md`). Brief ws-01 nie dzieli
pracy na kamienie, więc M1 = podzbiór produktów potrzebny do demo M1; reszta briefu jest niżej jako
"poza M1".

## M1: zrobione

Sprawdzone `make clean && make up && make smoke` (wszystkie sprawdzenia PASS, `make up` ok. 25 s przy
pobranych obrazach).

| Produkt | Stan |
| --- | --- |
| Keycloak 26.8.1, realm `corp` | klienci `claude-code` (public, PKCE S256 wymuszone, bez ROPC, `aud=mcp-gateway`), `mcp-gateway`, `mcp-portal-bff`, `mcp-credential-broker`, `mcp-gateway-admin`, `opensearch`; standardowy token exchange (gateway i BFF); mapper `groups` z `full.path=false`; użytkownicy `alice`, `bob`, `carol`, `mallory` z grupami C9 |
| OpenBao 2.7.2 (dev) | KV v2 `secret/`, Transit z kluczem `mcp-credential-broker`, AppRole brokera z polityką tylko na swoje ścieżki |
| Redis 8.8.4 | z hasłem |
| PostgreSQL 18.6 | bazy `gateway` i `broker`, osobni użytkownicy, brak dostępu krzyżowego |
| Mock Jiry (WireMock 3.13.2) | `/rest/api/2/myself` (C4.1) dla `<login>-pat`, inny PAT = inny użytkownik, 401 bez poświadczenia; projekty `ABC`, `DEF`, `HR` (`Restricted`), zgłoszenia z linkami między projektami, zgłoszenie przeniesione, wyszukiwanie po projekcie |
| Stuby C4 i C5 | WireMock z `mcp-contracts/mocks/wiremock`, tylko HTTPS z wymaganym certyfikatem klienta |
| CA i mTLS | CA EC P-256, certyfikaty `CN=<nazwa>`, SAN `<nazwa>.internal` dla gateway'a, brokera, BFF, CI, offboardingu i 5 serwerów MCP; PEM + PKCS12 dla Javy; certyfikat obcego CA do testów negatywnych |
| Skrypt zapisu PAT | `make pat USER=alice SYSTEM=jira [BROKER_URL=...]`: logowanie w kliencie BFF (PKCE), token exchange na `aud=mcp-credential-broker`, `PUT /v1/connections/{system}/secret` po mTLS z certyfikatem `mcp-portal-bff`; działa na stubie, na brokerze po zmianie `BROKER_URL` |
| Makefile | `up`, `up-core`, `up-full`, `down`, `reset`, `clean`, `certs`, `secrets`, `seed`, `token`, `claims`, `exchange`, `pat`, `smoke`, `logs`, `ps`, `users` |
| Profile compose | bez profilu (infrastruktura), `infra` (+ stuby), `full` (gateway, broker, `mcp-jira` z obrazów z `.env`; renderuje się, obrazów jeszcze nie ma) |
| Dokumentacja | `README.md`, `docs/porty.md`, `docs/uzytkownicy.md`, `docs/integracja.md`, `docs/claude-code.md` |

DoD briefu, punkty dotyczące M1:

- `make token USER=alice` zwraca JWT z `groups` bez ścieżki i `azp=claude-code`: tak (`make claims`).
- token exchange `alice` -> `aud=mcp-credential-broker`: tak, przez `mcp-gateway` i przez `mcp-portal-bff`.
- mock Jiry: `whoami` dla `alice-pat` -> `alice`, dla innego -> `someone-else`: tak.
- `make up` < 3 min, healthchecki zielone: tak przy pobranych obrazach; czasu z pustym cache obrazów
  (ok. 2,1 GB) nie mierzyłem.

## Odstępstwa od briefu

- Hasła użytkowników, sekrety klientów, root token OpenBao, hasła Redis/PostgreSQL i keystore'ów są
  generowane do `.secrets/dev.env` przy pierwszym `make up`, a nie zapisane w repo. Realm używa
  placeholderów `${...}` podstawianych przez Keycloak przy imporcie. Wyjątek: PAT-y mocka Jiry
  (`alice-pat` itd.) są stałe, bo wymaga ich DoD i nie dają dostępu do niczego poza mockiem.
- Keycloak w trybie dev bez trwałego wolumenu: realm jest importowany przy każdym starcie kontenera.
- Dodatkowy klient `opensearch` (cel token exchange) i opcjonalne client scope'y `*-audience`: Keycloak
  26.8 wymaga przy wymianie `scope=<audience>-audience` obok `audience` (bez tego `Requested audience not
  available`). Dzięki temu token logowania BFF ani `claude-code` nie ma audience brokera.
- `make token` nie używa ROPC: skrypt przechodzi prawdziwy authorization code + PKCE, wypełniając formularz
  logowania Keycloaka (bez przeglądarki).
- Komponenty w kontenerach muszą używać JWKS `http://keycloak:8080/...` i oczekiwanego `iss`
  `http://localhost:18080/realms/corp` zamiast discovery z `issuer-uri` (patrz `docs/porty.md`).
- Compose montuje stuby z katalogu roboczego `../mcp-contracts`, nie z tagu `v1.0.0`. W trakcie tej sesji
  ktoś miał w `mcp-contracts` niezacommitowane zmiany OpenAPI; katalog `mocks/` nie był zmieniony. Kto
  potrzebuje dokładnie `v1.0.0`: `git -C ../mcp-contracts worktree add ../mcp-contracts-v1.0.0 v1.0.0` i
  `MCP_CONTRACTS_DIR=../mcp-contracts-v1.0.0` w `.env`.

## Poza M1 (nie zrobione)

- step-up flow z poziomami `acr` (hasło / hasło+OTP); teraz `acr` = `1` dla każdego logowania,
- OpenSearch z security pluginem, JWT backend (`required_audience: opensearch`), indeksy, DLS/FLS i dwa
  punkty DoD o OpenSearch,
- mocki Confluence, GitLaba, service desku oraz serwery OAuth (authorize/token/refresh) dla GitLaba i Jiry,
- repozytorium paczek polityk (MinIO/nginx) i testowy klucz Ed25519,
- kolektor audytu ("SIEM"); do demo M1 audyt trzech komponentów trzeba na razie czytać z `make logs`,
- `make seed` zapisuje tylko PAT-y Jiry (alice, bob); seed OpenSearch dojdzie z OpenSearch,
- mock Jiry nie egzekwuje uprawnień per użytkownik (każdy PAT widzi wszystkie projekty).

## Niesprawdzone

- Prawdziwe logowanie Claude Code (`claude mcp add --client-id claude-code --callback-port 33418 ...`):
  bez gateway'a nie ma czego podłączyć. Sprawdzone elementy: PKCE, redirect `http://localhost:33418/callback`,
  `aud`, metadane RFC 8414 (także wariant z wstawioną ścieżką), tolerowanie parametru `resource`.
- Profil `full`: tylko `docker compose config`; nazwy zmiennych `MCP_*` dla komponentów to propozycja.
- `make up` na innym systemie niż macOS (Docker Desktop).

## Otwarte pytania

- CC-027 (zgłoszone do `mcp-contracts/CONTRACT_CHANGES.md`): tożsamość TLS (SAN) brokera i wewnętrznego
  endpointu gateway'a nie jest w kontraktach; środowisko przyjmuje `<nazwa>.internal`.
- Jakie wartości `acr` oznaczają MFA dla claimu `mfa` w C2 (Keycloak używa poziomów LoA, Entra innych
  wartości) - do ustalenia przed step-up flow w realmie.
- Stub `resolve` w `mcp-contracts` zwraca ten sam PAT (`jkowalski`) dla każdego użytkownika; demo M1 z
  tożsamością `alice` w Jirze wymaga prawdziwego brokera.
