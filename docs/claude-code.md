# Claude Code a lokalny gateway

Stan (2026-10-28): adres publiczny (M2) to `https://localhost:18745`, patrz sekcja "Przez proxy na https (M2)" na końcu; pierwsza część poniżej opisuje tryb M1 (`http://localhost:18000`, `make up-full`), który nadal przechodzi w `make m1`. Samego logowania
w przeglądarce nie wykonano; sprawdzone są metadane RFC 9728 i RFC 8414, żądanie autoryzacji z PKCE i `resource`,
redirect URI i odrzucenie obcego redirect URI. Pełna instrukcja dla człowieka: sekcja "Demo M1 z Claude Code" w README.

Anonimowa dynamiczna rejestracja klientów (DCR) jest zablokowana domyślną polityką Keycloaka "Trusted Hosts"
(`403 Host not trusted`, sprawdzone), więc Claude Code dostaje zarejestrowany wcześniej klient publiczny
`claude-code` i stały port callbacku (`CLAUDE_CODE_CALLBACK_PORT` w `.env`, domyślnie 33418):

```bash
make up-full      # albo gateway uruchomiony na hoście na porcie 18000
claude mcp add --transport http --client-id claude-code --callback-port 33418 \
  jira http://localhost:18000/s/mcp-jira/mcp
```

Przebieg:

1. Claude Code wywołuje gateway bez tokenu i dostaje `401` z
   `WWW-Authenticate: Bearer resource_metadata=".../.well-known/oauth-protected-resource/s/mcp-jira/mcp"`.
2. Z metadanych zasobu bierze `authorization_servers = ["https://localhost:18746/realms/corp"]` i pobiera
   `https://localhost:18746/.well-known/oauth-authorization-server/realms/corp` (w trybie M1 z `make up-full` Keycloak jest
   od 2026-10-28 też po https; zaufanie do CA: `make trust-ca`).
3. Otwiera przeglądarkę na logowanie Keycloak (login i hasło: `make users`), odbiera kod na
   `http://localhost:33418/callback`, wymienia go z `code_verifier`.
4. Token ma `aud=mcp-gateway`, `azp=claude-code`, `groups`; gateway go weryfikuje. Na stacji nie ma PAT:
   poświadczenie do Jiry gateway pobiera z brokera (najpierw `make pat USER=alice SYSTEM=jira` na
   prawdziwym brokerze).

Inny port callbacku: zmienić `CLAUDE_CODE_CALLBACK_PORT` w `.env` i zrestartować Keycloak (`make up`),
bo redirect URI w realmie jest podstawiany przy imporcie.

## Przez proxy na https (M2)

Adres publiczny to `https://localhost:18745` (docs/m2.md). Zaufanie do CA i `NODE_EXTRA_CA_CERTS` dla Node: `make trust-ca`
drukuje polecenia dla człowieka; bez nich `claude` odrzuci certyfikat. Serwer systemowy:

```bash
export NODE_EXTRA_CA_CERTS=<katalog repo>/certs/public-ca/ca.crt
claude mcp add --transport http --client-id claude-code --callback-port 33418 \
  jira https://localhost:18745/s/mcp-jira/mcp
```

Od M5 jeden adres obejmuje całą platformę (`https://localhost:18745/mcp`), a filtr jest w query, np.
`"https://localhost:18745/mcp?systems=jira&access=read"` (cudzysłów, bo adres ma `&`); kreator w portalu składa adres
(`GET /api/v1/me/mcp-endpoint`), niczego nie zapisuje i pokazuje gotową komendę
(BFF dostaje `PORTAL_CLIENT_CONFIGS_CLAUDE_CODE_CLIENT_ID=claude-code` i `..._CALLBACK_PORT=33418` z `docker-compose.yml`).
Metadane zasobu i wyzwanie 401 wskazują adres https; serwer autoryzacji to Keycloak po https, `https://localhost:18746/realms/corp`
(certyfikat tego samego CA "dev public", więc Safari zapisuje ciasteczka logowania; docs/m2.md, "Keycloak na https").
Klient `claude-code` w realmie `corp` jest publiczny, z redirect URI `http://localhost:33418/callback`.
