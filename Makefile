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

.PHONY: help secrets certs up up-core up-full down reset clean ps logs token claims exchange pat seed smoke users

help: ## list targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

secrets: ## generate .secrets/dev.env (once)
	@./scripts/init-secrets.sh

certs: secrets ## generate the dev CA and mTLS certificates (once)
	@./scripts/gen-certs.sh

up: certs ## start profile infra (infrastructure + C4/C5 contract stubs) and wait for health checks
	$(COMPOSE) --profile infra up -d --wait
	@$(COMPOSE) wait openbao-init >/dev/null  # --wait does not wait for one-shot jobs

up-core: certs ## start infrastructure only, without the C4/C5 stubs (for running own broker/gateway on the host)
	$(COMPOSE) up -d --wait
	@$(COMPOSE) wait openbao-init >/dev/null  # --wait does not wait for one-shot jobs

up-full: certs ## start profile full (needs images of gateway, broker, mcp-jira from .env)
	$(COMPOSE) --profile full up -d --wait
	@$(COMPOSE) wait openbao-init >/dev/null  # --wait does not wait for one-shot jobs

down: ## stop everything, keep volumes, secrets and certificates
	$(COMPOSE) --profile infra --profile full down

reset: ## drop containers and volumes (realm and databases are recreated), then start infra again
	$(COMPOSE) --profile infra --profile full down -v
	$(MAKE) up

clean: ## reset plus removal of generated secrets and certificates
	$(COMPOSE) --profile infra --profile full down -v
	rm -rf .secrets certs

ps: ## container status
	$(COMPOSE) --profile infra --profile full ps -a

logs: ## follow logs (SERVICE=keycloak to narrow)
	$(COMPOSE) --profile infra --profile full logs -f $(SERVICE)

token: ## access token of USER for claude-code (authorization code + PKCE)
	@$(PY) scripts/mcpdev.py token $(USER)

claims: ## decoded claims of USER's claude-code token
	@$(PY) scripts/mcpdev.py token $(USER) --claims

exchange: ## token exchange of USER's token to AUD (REQUESTER=mcp-portal-bff|mcp-gateway), decoded claims
	@$(PY) scripts/mcpdev.py exchange $(USER) --audience $(AUD) --requester $(REQUESTER) --claims

pat: ## store PAT of USER for SYSTEM through the broker API (SECRET=..., default <user>-pat; BROKER_URL=...)
	@$(PY) scripts/mcpdev.py put-secret $(USER) $(SYSTEM) $(if $(SECRET),--secret $(SECRET))

seed: ## store Jira PATs of alice and bob through the broker API
	@$(PY) scripts/mcpdev.py put-secret alice jira
	@$(PY) scripts/mcpdev.py put-secret bob jira

smoke: ## smoke test of the running profile infra
	@$(PY) scripts/smoke.py

users: ## test users and their generated passwords (NOT FOR PRODUCTION)
	@grep '^KC_USER_' .secrets/dev.env | sed -E 's/^KC_USER_([A-Z]+)_PASSWORD=/\1 /' | tr 'A-Z' 'a-z' | awk '{printf "  %-8s %s\n", $$1, $$2}'
	@grep '^KC_BOOTSTRAP_ADMIN' .secrets/dev.env | sed 's/^/  /'
