# Porty i adresy

Porty hosta są w `.env` i wiążą się tylko z `127.0.0.1`. W compose są dwie sieci:

- `mcp-dev-core`: Keycloak, OpenBao, PostgreSQL, Redis, mock Jiry, stuby C4/C5, broker, gateway;
- `mcp-dev-servers`: gateway, serwery MCP, Redis, mock Jiry. Serwer MCP nie widzi Keycloaka, OpenBao,
  PostgreSQL ani brokera (sprawdza `make smoke`).

Usługi mają nazwy usług i aliasy `*.internal`.

| Usługa | Z hosta | Z sieci compose | Uwagi |
| --- | --- | --- | --- |
| Keycloak (realm `corp`) | `http://localhost:18080` | `http://keycloak:8080` | `iss` zawsze `http://localhost:18080/realms/corp` (patrz niżej) |
| Konsola admina Keycloak | `http://localhost:18080/admin` | | login z `make users` |
| OpenBao (dev) | `http://localhost:18200` | `http://openbao:8200` | root token: `BAO_ROOT_TOKEN` w `.secrets/dev.env`; audyt na stdout kontenera |
| Redis | `localhost:16379` | `redis:6379` | ACL per komponent, patrz [integracja.md](integracja.md#redis-użytkownicy-i-prefiksy-propozycja-ws-01-dla-ws-02-ws-05-ws-06) |
| PostgreSQL | `localhost:15432` | `postgres:5432` | bazy `gateway` i `broker`, użytkownicy o tych samych nazwach |
| Mock Jiry (HTTP) | `http://localhost:18081` | `http://jira.internal:8080` | PAT jako `Authorization: Bearer <pat>`; `/__admin` otwarte (dziennik żądań do testów) |
| Stub brokera C4 (mTLS) | `https://localhost:18443` | `https://${MCP_BROKER_TLS_NAME}:8443` | `make up`; kontrakty z `MCP_CONTRACTS_REF` |
| Stub admina gateway'a C5 (mTLS) | `https://localhost:18444` | `https://${MCP_GATEWAY_INTERNAL_TLS_NAME}:8443` | `make up` |
| Gateway | `http://localhost:18000` | `${MCP_GATEWAY_INTERNAL_TLS_NAME}` | `make up-full`, port w kontenerze 8080 |
| Broker | `https://localhost:18100` | `https://${MCP_BROKER_TLS_NAME}:8443` | `make up-full` (stuby wtedy są usuwane) |
| `mcp-jira` | | `https://mcp-jira.internal:8443` | profil `full` |
| Portal (zarezerwowany) | `http://localhost:18090` | | redirect URI klienta `mcp-portal-bff` |
| Callback Claude Code | `http://localhost:33418/callback` | | redirect URI klienta `claude-code` |

## Keycloak: issuer i backchannel

`KC_HOSTNAME=http://localhost:18080` z `KC_HOSTNAME_BACKCHANNEL_DYNAMIC=true`. Skutek:

- `iss` w każdym tokenie to `http://localhost:18080/realms/corp`, także gdy token wydał kontener przez
  `http://keycloak:8080` (sprawdza to `make smoke`);
- discovery pobrane z sieci compose podaje `jwks_uri` i `token_endpoint` pod `http://keycloak:8080/...`;
- komponent w kontenerze **nie może** używać `issuer-uri` z discovery (w kontenerze `localhost` to on sam).
  Konfiguracja: JWKS `http://keycloak:8080/realms/corp/protocol/openid-connect/certs`, oczekiwany `iss`
  `http://localhost:18080/realms/corp`. W Spring Boot: `spring.security.oauth2.resourceserver.jwt.jwk-set-uri`
  plus `issuer-uri` (przy ustawionym `jwk-set-uri` Boot nie robi discovery, tylko sprawdza `iss`).
- komponent uruchomiony na hoście może używać zwykłego `issuer-uri`.

Metadane serwera autoryzacji (RFC 8414) są pod
`http://localhost:18080/realms/corp/.well-known/oauth-authorization-server` i w wariancie z wstawioną ścieżką
`http://localhost:18080/.well-known/oauth-authorization-server/realms/corp`.

## Certyfikaty (`certs/`, generuje `make certs`)

| Plik | Zawartość |
| --- | --- |
| `certs/ca/ca.crt` | wewnętrzne CA (EC P-256) |
| `certs/ca/truststore.p12` | CA jako PKCS12 dla Javy (wpis zaufany, `-jdktrust`); hasło `TLS_STORE_PASSWORD` |
| `certs/<nazwa>/tls.crt`, `tls.key`, `chain.crt` | certyfikat, klucz PKCS#8, certyfikat + CA |
| `certs/<nazwa>/keystore.p12` | klucz + łańcuch dla Javy; hasło `TLS_STORE_PASSWORD` |
| `certs/untrusted/mcp-gateway/` | CN `mcp-gateway` podpisany innym CA, do testów negatywnych |

| Nazwa | EKU | SAN |
| --- | --- | --- |
| `mcp-gateway` | serverAuth, clientAuth | `${MCP_GATEWAY_INTERNAL_TLS_NAME}`, `mcp-gateway`, `localhost`, `127.0.0.1` |
| `mcp-credential-broker` | serverAuth | `${MCP_BROKER_TLS_NAME}`, `mcp-credential-broker`, `localhost`, `127.0.0.1` |
| `mcp-jira`, `mcp-confluence`, `mcp-gitlab`, `mcp-servicedesk`, `mcp-opensearch` | serverAuth | `<serverId>.internal`, `<serverId>`, `localhost`, `127.0.0.1` |
| `mcp-portal-bff`, `mcp-registry-ci`, `mcp-offboarding` | clientAuth | `<nazwa>` |

Wszystkie mają `CN=<nazwa>`. `localhost` w SAN dostają tylko certyfikaty serwerowe, dla komponentów
uruchomionych na hoście; produkcyjne certyfikaty go nie mają. Klient i tak powinien weryfikować nazwę
z konfiguracji (`MCP_BROKER_TLS_NAME`, `MCP_GATEWAY_INTERNAL_TLS_NAME`), nie adres, pod który się łączy.
Kontenery stubów montują tylko swój keystore, CA (`ca.crt`, `truststore.p12`) i certyfikat klienta do
healthchecku; klucz CA i klucze innych tożsamości zostają na hoście.
