# mcp-platform-dev. Run `make help`.
SHELL := /bin/bash
COMPOSE := ./scripts/compose.sh
PY := python3
# USER is also the login name in the shell environment; only a value given to make counts.
ifneq ($(filter environment default undefined,$(origin USER)),)
USER := alice
endif
SYSTEM ?= jira
AUD ?= mcp-credential-broker
REQUESTER ?= mcp-portal-bff
STUBS := broker-mock gateway-admin-mock
COMPONENTS := mcp-gateway mcp-credential-broker mcp-jira

.PHONY: help secrets certs contracts up up-core up-full down reset clean ps logs token claims exchange pat seed \
        smoke users audit jira-auth images m1 images-m2 up-m2 m2-sync proxy-test trust-ca mocks-test m2 mock-auth \
        m3 otp reset-authenticator

help: ## list targets
	@grep -E '^[a-z0-9-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

secrets: ## generate .secrets/dev.env (missing keys only)
	@./scripts/init-secrets.sh

certs: secrets ## generate the dev CA, mTLS certificates and the https certificate of the proxy (once)
	@./scripts/gen-certs.sh
	@./scripts/gen-public-cert.sh

contracts: ## extract C4/C5 stubs and audit vectors of MCP_CONTRACTS_REF into out/contracts
	@./scripts/fetch-contracts.sh

# Stubs and real components share network aliases; only one of each pair may run.
up: certs contracts ## start profile infra (infrastructure + C4/C5 contract stubs) and wait for health checks
	@$(COMPOSE) --profile full rm -sf $(COMPONENTS) >/dev/null 2>&1
	$(COMPOSE) --profile infra up -d --wait --force-recreate $(STUBS)
	$(COMPOSE) --profile infra up -d --wait
	@./scripts/wait-init.sh

up-core: certs ## start infrastructure only, without the C4/C5 stubs (for running own broker/gateway on the host)
	@$(COMPOSE) --profile infra --profile full rm -sf $(STUBS) $(COMPONENTS) >/dev/null 2>&1
	$(COMPOSE) up -d --wait
	@./scripts/wait-init.sh

up-full: certs ## start profile full (needs images of gateway, broker, mcp-jira from .env); stops the stubs
	@$(COMPOSE) --profile infra rm -sf $(STUBS) >/dev/null 2>&1
	$(COMPOSE) --profile full up -d --wait
	@./scripts/wait-init.sh

down: ## stop everything, keep volumes, secrets and certificates
	$(COMPOSE) --profile infra --profile full --profile m2 down

reset: ## drop containers and volumes (realm and databases are recreated), then start infra again
	$(COMPOSE) --profile infra --profile full --profile m2 down -v
	$(MAKE) up

clean: ## reset plus removal of generated secrets, certificates and extracted contracts
	-$(COMPOSE) --profile infra --profile full --profile m2 down -v
	rm -rf .secrets certs out

ps: ## container status
	$(COMPOSE) --profile infra --profile full --profile m2 ps -a

logs: ## follow logs (SERVICE=keycloak to narrow)
	$(COMPOSE) --profile infra --profile full --profile m2 logs -f $(SERVICE)

token: ## access token of USER for claude-code (authorization code + PKCE)
	@$(PY) scripts/mcpdev.py token $(USER)

claims: ## decoded claims of USER's claude-code token
	@$(PY) scripts/mcpdev.py token $(USER) --claims

exchange: ## token exchange of USER's token to AUD (REQUESTER=mcp-portal-bff|mcp-gateway), decoded claims
	@$(PY) scripts/mcpdev.py exchange $(USER) --audience $(AUD) --requester $(REQUESTER) --claims

# SECRET reaches the script through the environment (exported by make), not through argv.
pat: export MCP_PAT := $(SECRET)
pat: ## store PAT of USER for SYSTEM via the broker API; stub by default, real broker: BROKER_URL=https://localhost:18100
	@$(PY) scripts/mcpdev.py put-secret $(USER) $(SYSTEM)

seed: ## store Jira PATs of alice and bob through the broker API (BROKER_URL as for pat)
	@$(PY) scripts/mcpdev.py put-secret alice jira
	@$(PY) scripts/mcpdev.py put-secret bob jira

audit: ## C8 events of one request from container logs: make audit RID=<id> [LOGS="host1.log host2.log"]
	@$(PY) scripts/mcpdev.py audit $(RID) $(if $(LOGS),--compose $(foreach f,$(LOGS),--logs $(f)))

jira-auth: ## which credentials reached the Jira mock (scheme and known PAT name; JWTs are flagged)
	@$(PY) scripts/mcpdev.py jira-auth

images: ## build the component images of profile full from git refs of the sibling repos (needs JAVA_HOME=JDK 25)
	@./scripts/build-images.sh

m1: ## M1 scenarios (F1 A1 A2 A3 A9 A15) against the running make up-full; needs ../mcp-e2e (read only)
	@$(PY) -I scripts/m1-compose.py

smoke: ## smoke test of the running profile infra
	@$(PY) scripts/smoke.py

users: ## test users and their generated passwords (NOT FOR PRODUCTION)
	@grep '^KC_USER_' .secrets/dev.env | sed -E 's/^KC_USER_([A-Z]+)_PASSWORD=/\1 /' | tr 'A-Z' 'a-z' | awk '{printf "  %-8s %s\n", $$1, $$2}'
	@grep '^KC_BOOTSTRAP_ADMIN' .secrets/dev.env | sed 's/^/  /'

images-m2: ## build the images of profile m2 (portal BFF and web, MCP servers of ws-04) from HEAD of the sibling repos
	@./scripts/build-images-m2.sh

# Compose recreates exactly the services whose configuration or image changed; m2-sync then applies what lives
# outside the containers' definitions (Keycloak client, Redis ACL, proxy rules).
up-m2: certs ## start profile m2 (proxy, portal BFF and web, MCP servers) on top of make up-full; recreates changed services only
	@$(COMPOSE) --profile infra rm -sf $(STUBS) >/dev/null 2>&1
	$(COMPOSE) --profile full --profile m2 up -d --wait
	@./scripts/wait-init.sh
	@$(MAKE) --no-print-directory m2-sync

m2-sync: ## apply the settings of the running stack that need no restart: Keycloak client, Redis ACL, nginx reload
	@./scripts/kc-sync-bff-client.sh
	@./scripts/redis-sync-acl.sh >/dev/null
	@$(COMPOSE) --profile m2 restart proxy

trust-ca: ## print the commands that trust the "dev public" CA (macOS keychain, Node/Claude Code); runs nothing
	@./scripts/trust-ca.sh

proxy-test: ## rules of the reverse proxy against stand-in upstreams (isolated network, does not touch the stack)
	@./scripts/proxy-test.sh

mocks-test: ## unit tests of the system mocks (GitLab, Confluence, Service Desk, OpenSearch)
	@cd mocks/systems && $(PY) -I -m unittest discover -s tests

m2: ## M2 scenario skeleton: the steps that can be checked on mocks and Keycloak; the rest is listed as SKIP
	@$(PY) scripts/m2.py run

mock-auth: ## credentials that reached a mock (never values): make mock-auth SYSTEM=gitlab
	@$(PY) scripts/m2.py mock-auth $(SYSTEM)

m3: ## M3 scenario skeleton: infrastructure checks plus the Authenticator / DESTRUCTIVE confirmation flow; SKIP where a component has no M3 yet
	@$(PY) scripts/m3.py run

otp: ## current TOTP code of a test user (USER=alice; seed from .secrets/dev.env); URI=1 prints the otpauth URI for a real app
	@$(PY) scripts/mcpdev.py otp $(USER) $(if $(URI),--uri)

reset-authenticator: ## remove the Authenticator registration of USER at the broker (mTLS CN=mcp-offboarding); BROKER_URL as for pat
	@$(PY) scripts/mcpdev.py reset-authenticator $(USER)
