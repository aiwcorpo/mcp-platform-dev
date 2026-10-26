# Porty i adresy

Porty hosta są w `.env` i wiążą się tylko z `127.0.0.1`. W sieci compose (`mcp-dev`) usługi mają nazwy
usług i aliasy `*.internal`.

| Usługa | Z hosta | Z sieci compose | Uwagi |
| --- | --- | --- | --- |
| Keycloak (realm `corp`) | `http://localhost:18080` | `http://keycloak:8080` | `iss` zawsze `http://localhost:18080/realms/corp` (patrz niżej) |
| Konsola admina Keycloak | `http://localhost:18080/admin` | | login z `make users` |
| OpenBao (dev) | `http://localhost:18200` | `http://openbao:8200` | root token: `BAO_ROOT_TOKEN` w `.secrets/dev.env` |
| Redis | `localhost:16379` | `redis:6379` | hasło `REDIS_PASSWORD` |
| PostgreSQL | `localhost:15432` | `postgres:5432` | bazy `gateway` i `broker`, użytkownicy o tych samych nazwach |
| Mock Jiry (HTTP) | `http://localhost:18081` | `http://jira.internal:8080` | PAT jako `Authorization: Bearer <pat>` |
| Stub brokera C4 (mTLS) | `https://localhost:18443` | `https://mcp-credential-broker.internal:8443` | profil `infra` |
| Stub admina gateway'a C5 (mTLS) | `https://localhost:18444` | `https://mcp-gateway.internal:8443` | profil `infra` |
| Gateway | `http://localhost:18000` | `mcp-gateway.internal` | profil `full`, port w kontenerze 8080 |
| Broker | `https://localhost:18100` | `https://mcp-credential-broker.internal:8443` | profil `full` |
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

Nazwy: `mcp-gateway`, `mcp-credential-broker`, `mcp-portal-bff`, `mcp-registry-ci`, `mcp-offboarding`,
`mcp-jira`, `mcp-confluence`, `mcp-gitlab`, `mcp-servicedesk`, `mcp-opensearch`. Każdy certyfikat ma
`CN=<nazwa>`, SAN `DNS:<nazwa>.internal`, `DNS:<nazwa>`, `DNS:localhost`, `IP:127.0.0.1` oraz EKU
`serverAuth` i `clientAuth`. `localhost` w SAN jest wygodą dev (komponent na hoście); produkcyjne
certyfikaty go nie mają.
