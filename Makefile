# unity-gateway-setup — main task executor
#
# Run `make` or `make help` to list targets.

# ---- configuration (override on the command line, e.g. `make tf-plan TF_DIR=...`) ----
TF        ?= terraform
TF_ROOT   ?= terraform
TF_DIR    ?= terraform/infra
ARGS      ?=

PYTHON    ?= python3
PROFILE   ?= fevm-west
AGENT_GEN ?= agent_setups/scripts/generate.py

.DEFAULT_GOAL := help

# ---- meta ----

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ---- terraform ----

.PHONY: tf-fmt
tf-fmt: ## Format all Terraform files in place
	$(TF) fmt -recursive $(TF_ROOT)

.PHONY: tf-fmt-check
tf-fmt-check: ## Check Terraform formatting (no writes; non-zero if unformatted)
	$(TF) fmt -recursive -check -diff $(TF_ROOT)

.PHONY: tf-init
tf-init: ## Initialize the infra working directory (downloads providers, configures backend)
	$(TF) -chdir=$(TF_DIR) init -input=false $(ARGS)

.PHONY: tf-validate
tf-validate: ## Validate the infra configuration (no credentials/network required)
	$(TF) -chdir=$(TF_DIR) init -backend=false -input=false >/dev/null
	$(TF) -chdir=$(TF_DIR) validate

.PHONY: tf-plan
tf-plan: ## Show the execution plan against the target workspace (read-only)
	$(TF) -chdir=$(TF_DIR) plan -input=false $(ARGS)

.PHONY: tf-apply
tf-apply: ## Apply the configuration (prompts for confirmation)
	$(TF) -chdir=$(TF_DIR) apply -input=false $(ARGS)

.PHONY: tf-destroy
tf-destroy: ## Destroy the managed resources (prompts for confirmation)
	$(TF) -chdir=$(TF_DIR) destroy -input=false $(ARGS)

.PHONY: tf-output
tf-output: ## Show the infra outputs
	$(TF) -chdir=$(TF_DIR) output $(ARGS)

.PHONY: tf-check
tf-check: tf-fmt-check tf-validate ## Run fmt-check + validate (CI-friendly)

.PHONY: tf-clean
tf-clean: ## Remove local Terraform working artifacts (.terraform, lock, plan files)
	find $(TF_ROOT) -type d -name '.terraform' -prune -exec rm -rf {} +
	find $(TF_ROOT) -type f \( -name '.terraform.lock.hcl' -o -name '*.tfplan' \) -delete

# ---- agent configs ----

.PHONY: agent-claude-code
agent-claude-code: ## Generate Claude Code managed-settings.json from TF outputs (PROFILE=, ARGS=)
	$(PYTHON) $(AGENT_GEN) claude-code --profile $(PROFILE) $(ARGS)

.PHONY: agent-claude-code-preview
agent-claude-code-preview: ## Print the generated Claude Code managed-settings.json without writing
	$(PYTHON) $(AGENT_GEN) claude-code --profile $(PROFILE) --stdout $(ARGS)

# ---- docker test harness ----
# Isolated container to test the generated managed-settings.json (routing + OTEL
# telemetry) without touching the host's own managed settings. Typical flow:
#   make tf-apply                 # provision the telemetry infra (once)
#   make docker-build             # build the image (once)
#   make docker-config            # generate config with Linux helper path
#   make docker-up                # start the container
#   make docker-login             # databricks auth login inside (browser on host)
#   make docker-shell             # exec in; run `claude` to generate telemetry

DOCKER_IMAGE     ?= unity-gateway-test
DOCKER_CONTAINER ?= unity-gateway-test
CONTAINER_CFG    ?= agent_setups/generated/container
# Workspace host for PROFILE, read from ~/.databrickscfg at parse time.
WS_HOST := $(shell $(PYTHON) -c "import configparser,pathlib; c=configparser.ConfigParser(); c.read(str(pathlib.Path.home()/'.databrickscfg')); print(c.get('$(PROFILE)','host',fallback=''))")
# Forward a non-default npm registry (e.g. a corporate mirror) into the build so
# `npm install` works behind it; teammates on the public registry pass nothing.
NPM_REGISTRY     := $(shell npm config get registry 2>/dev/null)
NPM_REGISTRY_ARG := $(if $(filter-out https://registry.npmjs.org/,$(NPM_REGISTRY)),--build-arg NPM_REGISTRY=$(NPM_REGISTRY),)

.PHONY: docker-build
docker-build: ## Build the test-harness image (Claude Code + databricks CLI + python3)
	docker build -t $(DOCKER_IMAGE) $(NPM_REGISTRY_ARG) docker/

.PHONY: docker-config
docker-config: ## Generate Claude Code config for the container (Linux helper path + full model picker); needs applied telemetry infra
	$(PYTHON) $(AGENT_GEN) claude-code --profile $(PROFILE) \
		--otel-helper-install-path /etc/claude-code/otel-headers-helper.sh \
		--model-picker \
		--out-dir $(CONTAINER_CFG) $(ARGS)

.PHONY: docker-reload
docker-reload: docker-config ## Regenerate config and copy it into the RUNNING container (no restart, keeps auth)
	docker cp $(CONTAINER_CFG)/claude-code/managed-settings.json $(DOCKER_CONTAINER):/etc/claude-code/managed-settings.json
	docker cp $(CONTAINER_CFG)/claude-code/otel-headers-helper.sh $(DOCKER_CONTAINER):/etc/claude-code/otel-headers-helper.sh
	docker exec -u root $(DOCKER_CONTAINER) chmod +x /etc/claude-code/otel-headers-helper.sh
	@echo "Config reloaded. Restart your \`claude\` session (exit and re-run) to pick it up."

.PHONY: docker-up
docker-up: ## Start the container (mounts config, maps OAuth port 8020, writes the profile)
	@test -f "$(CONTAINER_CFG)/claude-code/managed-settings.json" \
		|| { echo "No config at $(CONTAINER_CFG)/claude-code/ — run 'make docker-config' first."; exit 1; }
	@test -n "$(WS_HOST)" \
		|| { echo "Could not resolve host for profile '$(PROFILE)' in ~/.databrickscfg."; exit 1; }
	docker run -d --name $(DOCKER_CONTAINER) \
		-p 8020:8020 \
		-e DATABRICKS_WS_HOST="$(WS_HOST)" \
		-e DATABRICKS_PROFILE_NAME="$(PROFILE)" \
		-e DATABRICKS_CONFIG_PROFILE="$(PROFILE)" \
		-v "$(abspath $(CONTAINER_CFG)/claude-code)":/opt/agent-config:ro \
		$(DOCKER_IMAGE)
	@echo ""
	@echo "Container '$(DOCKER_CONTAINER)' up (profile '$(PROFILE)' -> $(WS_HOST))."
	@echo "  make docker-login   # authenticate (opens a URL to paste into your host browser)"
	@echo "  make docker-shell   # then run: claude"

.PHONY: docker-login
docker-login: ## Run `databricks auth login` inside the container (default profile)
	docker exec -it -u dev $(DOCKER_CONTAINER) databricks auth login --profile $(PROFILE)

.PHONY: docker-shell
docker-shell: ## Open an interactive shell in the container (as the dev user)
	docker exec -it -u dev -w /home/dev/work $(DOCKER_CONTAINER) bash

.PHONY: docker-logs
docker-logs: ## Show the container's startup log (entrypoint + socat)
	docker logs $(DOCKER_CONTAINER)

.PHONY: docker-down
docker-down: ## Stop and remove the container
	docker rm -f $(DOCKER_CONTAINER)

.PHONY: docker-test
docker-test: docker-build docker-config docker-up ## Build + config + up in one step
