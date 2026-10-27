# Użytkownicy, grupy i klienci (realm `corp`)

Źródło: `keycloak/realm-corp.json`, importowany przy każdym starcie kontenera (dane Keycloak nie są
trwałe, więc realm zawsze odpowiada plikowi). Hasła są w `.secrets/dev.env`, `make users` je wypisuje.

## Użytkownicy

| Login | Grupy (C9) | Do czego |
| --- | --- | --- |
| `alice` | `mcp-jira-read`, `mcp-jira-write`, `mcp-jira-proj-ABC`, `mcp-confluence-read`, `mcp-confluence-space-ENG`, `mcp-gitlab-read`, `mcp-gitlab-write`, `mcp-gitlab-grp-team-a`, `mcp-opensearch-read`, `mcp-opensearch-idx-logs-team-a`, `mcp-client-claude-code` | główna ścieżka demo |
| `bob` | `mcp-jira-read`, `mcp-jira-proj-ABC`, `mcp-opensearch-read`, `mcp-opensearch-idx-logs-team-a`, `mcp-client-claude-code` | tylko odczyt |
| `carol` | `mcp-platform-admin`, `mcp-client-claude-code` | admin platformy |
| `mallory` | brak | brak uprawnień |

Adresy e-mail `<login>@corp.test`. Grupy bez użytkowników, przygotowane do testów zakresów:
`mcp-jira-destructive`, `mcp-jira-proj-DEF`, `mcp-jira-proj-HR`, `mcp-confluence-write`,
`mcp-servicedesk-read`, `mcp-servicedesk-write`, `mcp-servicedesk-queue-IT`, `mcp-opensearch-idx-logs-team-b`.

Claim `groups` zawiera nazwy bez ścieżki (`mcp-jira-read`, nie `/mcp-jira-read`): mapper
`oidc-group-membership-mapper` z `full.path=false` (CC-007) w client scope `groups`.

## Klienci OIDC

| Klient | Typ | Przepływy | Uwagi |
| --- | --- | --- | --- |
| `claude-code` | public | authorization code + PKCE S256 (wymuszone) | redirect `http://localhost:33418/callback` i `http://127.0.0.1:33418/callback`; zawsze `aud=mcp-gateway`; bez ROPC |
| `mcp-gateway` | confidential | tylko token exchange | wymienia token z `aud=mcp-gateway` wyłącznie na `mcp-credential-broker` (`subjectToken` dla `resolve`, C4) |
| `mcp-portal-bff` | confidential | authorization code (+PKCE), token exchange | redirect `http://localhost:18090/login/oauth2/code/*`, `.../api/v1/oauth/callback`; wymiana na `mcp-credential-broker` albo `mcp-gateway-admin` |
| `mcp-credential-broker` | confidential | tylko token exchange | `aud` tokenów C4; wymienia `subjectToken` (aud brokera) na `opensearch` dla systemów typu `token-exchange` |
| `mcp-gateway-admin` | confidential | brak | tylko jako `aud` |
| `opensearch` | confidential | brak | tylko jako `aud` (typ poświadczenia `token-exchange`) |

Sekrety klientów: `KC_CLIENT_*_SECRET` w `.secrets/dev.env`.

Token `claude-code` zawiera `iss`, `sub`, `aud`, `exp`, `iat`, `azp`, `preferred_username`, `groups`,
`acr`, `auth_time` (C1); czas życia 15 min. Client scope `roles` nie jest przypisany, więc w tokenie nie ma
`realm_access` ani `aud=account`.

## Token exchange (standard, RFC 8693)

Łańcuch wymian zgodny z C4: gateway wymienia token klienta AI na `aud=mcp-credential-broker` i przekazuje go
w `resolve` jako `subjectToken`; dopiero broker wymienia go na audience systemu docelowego (`opensearch`).
BFF wymienia swój token sesji na `mcp-credential-broker` albo `mcp-gateway-admin`.

| Wymieniający | Token wejściowy | Dozwolone `audience` |
| --- | --- | --- |
| `mcp-gateway` | `claude-code` (`aud=mcp-gateway`) | `mcp-credential-broker` |
| `mcp-portal-bff` | własny token sesji | `mcp-credential-broker`, `mcp-gateway-admin` |
| `mcp-credential-broker` | `aud=mcp-credential-broker` | `opensearch` |

Audience docelowe są w opcjonalnych client scope'ach (`mcp-credential-broker-audience`,
`mcp-gateway-admin-audience`, `opensearch-audience`). Zwykły token logowania ich nie ma; wymiana musi podać
i `audience`, i odpowiadający scope:

```bash
curl -s http://localhost:18080/realms/corp/protocol/openid-connect/token \
  -d grant_type=urn:ietf:params:oauth:grant-type:token-exchange \
  -d client_id=mcp-gateway -d client_secret="$KC_CLIENT_MCP_GATEWAY_SECRET" \
  -d subject_token="$USER_TOKEN" \
  -d subject_token_type=urn:ietf:params:oauth:token-type:access_token \
  -d audience=mcp-credential-broker -d scope=mcp-credential-broker-audience
```

Bez `scope` Keycloak 26.8 odpowiada `Requested audience not available`. Wynik: `aud=mcp-credential-broker`,
ten sam `sub`, `azp` = klient wymieniający. Sprawdzane w `make smoke`:

- działa: gateway -> broker, BFF -> broker, broker (token od gateway'a) -> `opensearch`;
- odmowa: gateway -> `opensearch`, gateway -> `mcp-gateway-admin`, gateway z tokenem brokera -> `opensearch`,
  `claude-code` (klient publiczny) -> cokolwiek, BFF z tokenem `claude-code`, BFF z tokenem wydanym gateway'owi,
  `claude-code` proszący o scope brokera przy logowaniu;
- wymiana przez gateway bez `audience` daje token bez żadnego audience komponentu (bezużyteczny dla odbiorców).

Anonimowa dynamiczna rejestracja klientów jest zablokowana jawnie: realm deklaruje politykę "Trusted Hosts"
z pustą listą hostów (`components` w `realm-corp.json`).

## Mock Jiry: PAT i tożsamości (C4.1)

| `Authorization` | `GET /rest/api/2/myself` zwraca `name` |
| --- | --- |
| `Bearer alice-pat` / `bob-pat` / `carol-pat` / `mallory-pat` | `alice` / `bob` / `carol` / `mallory` |
| `Bearer test-only-jira-pat-0000000000000000` | `jkowalski` (wartość ze stuba `resolve` w `mcp-contracts`) |
| inny `Bearer ...` | `someone-else` (do testu `identity_mismatch`) |
| brak | `401` |

Dane: projekty `ABC`, `DEF`, `HR` (kategoria `Restricted`); zgłoszenia `ABC-1` (linki do `HR-1` i `DEF-1`),
`ABC-2`, `DEF-1`, `DEF-5`, `HR-1`; `ABC-3` przeniesione do `DEF` (odpowiedź z kluczem `DEF-5`);
`/rest/api/2/search` (GET i POST) rozpoznaje `project = ABC|DEF|HR` w JQL. Endpointy danych wymagają
jednego ze znanych PAT z tabeli. Każde inne poświadczenie, w tym token z Keycloaka (token passthrough,
niezmiennik 2), cudzy PAT i `Basic`, dostaje `401`. `make jira-auth` pokazuje z dziennika żądań mocka, jakie
poświadczenie dotarło do Jiry (nazwa znanego PAT, oznaczony JWT z `iss`/`aud`, inne tylko długością).
Mock nie sprawdza uprawnień per użytkownik.
