# Claude Code a lokalny gateway

Stan: realm jest gotowy, gateway'a jeszcze nie ma (ws-05). Poniższe polecenia nie były uruchomione
end-to-end; sprawdzone są ich elementy: klient `claude-code` z PKCE, redirect, `aud`, metadane RFC 8414
Keycloaka i tolerowanie parametru `resource` (RFC 8707) w żądaniu autoryzacji i tokenu.

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
2. Z metadanych zasobu bierze `authorization_servers = ["http://localhost:18080/realms/corp"]` i pobiera
   `http://localhost:18080/.well-known/oauth-authorization-server/realms/corp`.
3. Otwiera przeglądarkę na logowanie Keycloak (login i hasło: `make users`), odbiera kod na
   `http://localhost:33418/callback`, wymienia go z `code_verifier`.
4. Token ma `aud=mcp-gateway`, `azp=claude-code`, `groups`; gateway go weryfikuje. Na stacji nie ma PAT:
   poświadczenie do Jiry gateway pobiera z brokera (najpierw `make pat USER=alice SYSTEM=jira` na
   prawdziwym brokerze).

Inny port callbacku: zmienić `CLAUDE_CODE_CALLBACK_PORT` w `.env` i zrestartować Keycloak (`make up`),
bo redirect URI w realmie jest podstawiany przy imporcie.
