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
# The services of the M5 layer join the list while it is active, so `make up` does not leave them behind.
M5_ACTIVE := $(shell grep -s '^M5_ACTIVE=' .env | cut -d= -f2-)
COMPONENTS := mcp-gateway mcp-credential-broker mcp-jira $(if $(filter 1,$(M5_ACTIVE)),mcp-gateway-2 mcp-credential-broker-2 gateway-lb broker-lb)

.PHONY: help secrets certs contracts up up-core up-full down reset clean ps logs token claims exchange pat seed \
        smoke users audit jira-auth images m1 images-m2 up-m2 m2-sync proxy-test trust-ca mocks-test m2 mock-auth \
        m3 otp reset-authenticator registry-refresh admin-password m3-switch \
        m4 m4-test m4-switch offboard registry-register openbao-test \
        m5-switch m5-rollback m5-artifacts ci ci-local lb-test rollback-test compose-check start-test mock-fault c2-keys openbao-init openbao-renew openbao-persist-test openbao-migrate openbao-guard

help: ## list targets
	@grep -E '^[a-z0-9-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

secrets: ## generate .secrets/dev.env (missing keys only)
	@./scripts/init-secrets.sh

certs: secrets ## generate the dev CA, mTLS certificates (CI certificate per server), https certificate of the proxy (once)
	@./scripts/gen-certs.sh
	@./scripts/gen-public-cert.sh

contracts: ## extract C4/C5 stubs and audit vectors of MCP_CONTRACTS_REF into out/contracts
	@./scripts/fetch-contracts.sh

# Stubs and real components share network aliases; only one of each pair may run.
up: openbao-guard certs contracts ## start profile infra (infrastructure + C4/C5 contract stubs) and wait for health checks
	@$(COMPOSE) --profile full rm -sf $(COMPONENTS) >/dev/null 2>&1
	$(COMPOSE) --profile infra up -d --wait --force-recreate $(STUBS)
	$(COMPOSE) --profile infra up -d --wait
	@./scripts/wait-init.sh

up-core: openbao-guard certs ## start infrastructure only, without the C4/C5 stubs (for running own broker/gateway on the host)
	@$(COMPOSE) --profile infra --profile full rm -sf $(STUBS) $(COMPONENTS) >/dev/null 2>&1
	$(COMPOSE) up -d --wait
	@./scripts/wait-init.sh

up-full: openbao-guard certs ## start profile full (needs images of gateway, broker, mcp-jira from .env); stops the stubs
	@$(COMPOSE) --profile infra rm -sf $(STUBS) >/dev/null 2>&1
	$(COMPOSE) --profile full up -d --wait
	@./scripts/wait-init.sh

down: ## stop everything, keep volumes, secrets and certificates (OpenBao data included)
	$(COMPOSE) --profile infra --profile full --profile m2 down

reset: ## drop containers and volumes (realm, databases and ALL OPENBAO SECRETS are recreated empty), then start infra again
	$(COMPOSE) --profile infra --profile full --profile m2 down -v
	BAO_ALLOW_REINIT=1 $(MAKE) up

clean: ## reset plus removal of generated secrets, certificates and extracted contracts
	$(COMPOSE) --profile infra --profile full --profile m2 down -v || { echo "make clean: down -v failed; .secrets is kept, because without it the OpenBao volume could no longer be unsealed" >&2; exit 1; }
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

smoke: ## smoke test of the running stack (contract stubs or real broker and gateway, detected; writes no user data in the real mode)
	@$(PY) scripts/smoke.py

users: ## test users and their generated passwords (NOT FOR PRODUCTION)
	@grep -E '^KC_USER_[A-Z]+_PASSWORD=' .secrets/dev.env | sed -E 's/^KC_USER_([A-Z]+)_PASSWORD=/\1 /' | tr 'A-Z' 'a-z' | awk '{printf "  %-8s %s\n", $$1, $$2}'
	@echo '  (OTP of the test users: make otp USER=alice prints the current code, never the seed; admin console: make admin-password)'

images-m2: ## build the images of profile m2 (portal BFF and web, MCP servers of ws-04) from HEAD of the sibling repos
	@./scripts/build-images-m2.sh

# Compose recreates exactly the services whose configuration or image changed; m2-sync then applies what lives
# outside the containers' definitions (Keycloak client, Redis ACL, proxy rules).
up-m2: openbao-guard certs ## start profile m2 (proxy, portal BFF and web, MCP servers) on top of make up-full; recreates changed services only
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

reset-authenticator: ## remove the Authenticator registration of USER at the broker (mTLS CN=mcp-offboarding); alice, bob, carol, mallory only with FORCE=1; BROKER_URL as for pat
	@$(PY) scripts/mcpdev.py reset-authenticator $(USER) $(if $(FORCE),--force)

registry-refresh: ## dev only: drop the manifests the gateway took over by bootstrap and let it fetch them again (after a tool definition changed)
	@./scripts/registry-refresh.sh

admin-password: ## login and password of the Keycloak admin console (master realm; it can change the step-up flow, so it is not part of make users)
	@grep '^KC_BOOTSTRAP_ADMIN' .secrets/dev.env | sed 's/^/  /'

m3-switch: ## apply the prepared M3 switch to the running stack (broker, MCP servers, gateway recreated from .env; state is kept)
	@./scripts/apply-m3-switch.sh

m4: ## M4 infrastructure checks (CI certificates, dev- keys, token exchange, service account); steps that need M4 components are SKIP before the switch
	@$(PY) scripts/m4.py run

m4-test: ## unit tests of offboarding and registry registration against fake Keycloak, broker and gateway (needs make certs; no stack)
	@$(PY) -I -m unittest discover -s scripts/tests

openbao-test: ## openbao/init.sh against a throw-away OpenBao: service account of Service Desk and the broker policy (isolated network)
	@./scripts/openbao-init-test.sh

offboard: ## offboarding of USER=<login>: IdP groups and logout, broker purge, gateway purge (CN mcp-offboarding), purges repeated; DRY=1 only prints the plan
	@$(PY) scripts/mcpdev.py offboard $(USER) $(if $(DRY),--dry-run)

registry-register: ## PUT the manifests built from the server repositories with the certificate of each server (CN mcp-registry-ci-<serverId>); SERVERS="mcp-jira ..." narrows
	@./scripts/registry-register.sh

m4-switch: ## apply the prepared M4 switch to the running stack (broker, servers, registry by CI, gateway, BFF/web; state is kept); CHECK=1 only checks the preconditions
	@./scripts/apply-m4-switch.sh $(if $(CHECK),--check)

m5-switch: ## apply the prepared M5 switch to the running stack (two gateways and brokers, balancers, /mcp routes; state is kept); CHECK=1 only checks, CONFIRM=1 does it; only on the order of the coordinator
	@./scripts/apply-m5-switch.sh $(if $(CHECK),--check,$(if $(CONFIRM),--confirm))

m5-rollback: ## way back from a half done or rejected M5 switch: make m5-rollback BACKUP=out/m5-backup-<time> CONFIRM=1 (without CONFIRM only the plan)
	@./scripts/rollback-m5.sh $(if $(CONFIRM),--confirm,--dry-run) $(BACKUP)

rollback-test: ## database part of the M5 rollback on a throw-away PostgreSQL (isolated network, does not touch the stack)
	@./scripts/rollback-test.sh

openbao-guard:
	@./scripts/openbao-guard.sh

openbao-persist-test: ## dev OpenBao on a throw-away instance: data, Transit keys and AppRole survive restart and Stop/Start, unseal sidecar, init.sh overwrites nothing (isolated network and volumes, about 2 min)
	@./scripts/openbao-persist-test.sh

openbao-migrate: ## ONE-OFF switch of the running stack to the persistent OpenBao (volume openbao-data); ALL CURRENT OPENBAO SECRETS ARE LOST; CHECK=1 only plans, CONFIRM=1 (exactly 1) does it; only on the order of the coordinator
	@./scripts/openbao-migrate.sh $(if $(CHECK),--check,$(if $(filter 1,$(CONFIRM)),--confirm))

openbao-init: ## run openbao/init.sh against the stack's OpenBao; an expired AppRole secret_id of the broker is recreated, a valid one is left alone; restarts nothing
	$(COMPOSE) --profile full --profile m2 run --rm --no-deps openbao-init

openbao-renew: ## replace the AppRole secret_id of the broker even if still valid, so its TTL (30 days) starts again (docs/m5.md); touches OpenBao only, restarts nothing
	BAO_RENEW_SECRET_ID=1 $(COMPOSE) --profile full --profile m2 run --rm --no-deps openbao-init

m5-artifacts: ## start the package server (18756) and the image registry (18757) of D12; touches only these two services
	@mkdir -p artifacts/packages
	M5_ACTIVE=1 $(COMPOSE) --profile ci run --rm --no-deps registry-data-init
	M5_ACTIVE=1 $(COMPOSE) --profile ci up -d --no-deps --wait packages registry

ci: ## the CI entry point (ci/run.sh): lint, unit, compose, style, proxy, lb; STEPS="lint unit" narrows
	@./ci/run.sh $(STEPS)

ci-local: ## the same through the local GitLab CI runner in a container (ci/local-runner.sh); JOB=static narrows; needs docker build
	@./ci/local-runner.sh $(JOB)

lb-test: ## balancers of gateway and broker against stand-in instances (isolated network, does not touch the stack)
	@./scripts/lb-test.sh

compose-check: ## rendered compose configuration of the M3, M4 and M5 layers (nothing is started)
	@$(PY) -I scripts/check-compose.py

start-test: ## compose start after the one-shot job container is gone (own throw-away project, does not touch the stack, about 40 s)
	@./scripts/start-test.sh

mock-fault: ## latency and 429 of a system mock: make mock-fault ARGS="set gitlab latencyMs=200 rateLimitPerSecond=20" (show / clear <system>|all)
	@$(PY) scripts/mock-fault.py $(ARGS)

c2-keys: ## shared private JWKS of the internal JWT for the M5 gateway instances (.secrets/c2-jwks.json; kept when it exists)
	@./scripts/gen-c2-keys.sh
