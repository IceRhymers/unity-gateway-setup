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
# Where generated configs land. The docker-config* targets override this to the
# container dir so the harness tests exactly what `agent-*` generates.
OUT_DIR   ?= agent_setups/generated

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
agent-claude-code: ## Generate Claude Code managed-settings.json from TF outputs (PROFILE=, OUT_DIR=, ARGS=)
	$(PYTHON) $(AGENT_GEN) claude-code --profile $(PROFILE) --out-dir $(OUT_DIR) $(ARGS)

.PHONY: agent-claude-code-preview
agent-claude-code-preview: ## Print the generated Claude Code managed-settings.json without writing
	$(PYTHON) $(AGENT_GEN) claude-code --profile $(PROFILE) --stdout $(ARGS)

.PHONY: agent-codex
agent-codex: ## Generate Codex config.toml from TF outputs (PROFILE=, OUT_DIR=, ARGS=)
	$(PYTHON) $(AGENT_GEN) codex --profile $(PROFILE) --out-dir $(OUT_DIR) $(ARGS)

.PHONY: agent-codex-preview
agent-codex-preview: ## Print the generated Codex config.toml without writing
	$(PYTHON) $(AGENT_GEN) codex --profile $(PROFILE) --stdout $(ARGS)

# ---- docker test harness ----
# Isolated container to test the generated agent configs (Claude Code routing +
# OTEL telemetry, and Codex gateway routing) without touching the host's own
# settings. Typical flow:
#   make tf-apply                 # provision the telemetry infra (once)
#   make docker-build             # build the image (once)
#   make docker-config-all        # generate both agent configs (or -config / -config-codex)
#   make docker-up                # start the container
#   make docker-login             # databricks auth login inside (browser on host)
#   make docker-shell             # exec in; run `claude` or `codex` to generate traffic

DOCKER_IMAGE     ?= unity-gateway-test
DOCKER_CONTAINER ?= unity-gateway-test
CONTAINER_CFG    ?= agent_setups/generated/container
# Mount the Codex config only when it has been generated (docker-config-codex),
# so the harness works with either or both agents present.
CODEX_CFG_MOUNT  := $(if $(wildcard $(CONTAINER_CFG)/codex/config.toml),-v "$(abspath $(CONTAINER_CFG)/codex)":/opt/agent-config-codex:ro,)
# Workspace host for PROFILE, read from ~/.databrickscfg at parse time.
WS_HOST := $(shell $(PYTHON) -c "import configparser,pathlib; c=configparser.ConfigParser(); c.read(str(pathlib.Path.home()/'.databrickscfg')); print(c.get('$(PROFILE)','host',fallback=''))")
# Forward a non-default npm registry (e.g. a corporate mirror) into the build so
# `npm install` works behind it; teammates on the public registry pass nothing.
NPM_REGISTRY     := $(shell npm config get registry 2>/dev/null)
NPM_REGISTRY_ARG := $(if $(filter-out https://registry.npmjs.org/,$(NPM_REGISTRY)),--build-arg NPM_REGISTRY=$(NPM_REGISTRY),)
# Override the ucode install source (pinned ref, mirror, private URL with token,
# or a local path); empty uses the Dockerfile default (github.com/databricks/ucode).
UCODE_SOURCE     ?=
UCODE_SOURCE_ARG := $(if $(UCODE_SOURCE),--build-arg UCODE_SOURCE=$(UCODE_SOURCE),)
# Forward a PyPI proxy/mirror into the build so `uv tool install ucode` resolves
# ucode's build deps (hatchling, uv-dynamic-versioning) behind it — the pip-side
# counterpart to NPM_REGISTRY. Resolved from UV_DEFAULT_INDEX / UV_INDEX_URL, else
# the first URL in uv's global config; override with `make ... PYPI_INDEX=<url>`.
UV_CONFIG_FILE   := $(if $(XDG_CONFIG_HOME),$(XDG_CONFIG_HOME),$(HOME)/.config)/uv/uv.toml
PYPI_INDEX       ?= $(shell if [ -n "$$UV_DEFAULT_INDEX" ]; then echo "$$UV_DEFAULT_INDEX"; elif [ -n "$$UV_INDEX_URL" ]; then echo "$$UV_INDEX_URL"; elif [ -f "$(UV_CONFIG_FILE)" ]; then grep -Eo 'https?://[^"'"'"' ]+' "$(UV_CONFIG_FILE)" | head -1; fi)
PYPI_INDEX_ARG   := $(if $(PYPI_INDEX),--build-arg PYPI_INDEX=$(PYPI_INDEX),)

.PHONY: docker-build
docker-build: ## Build the test-harness image (Claude Code + Codex + databricks CLI + python3 + ucode)
	docker build -t $(DOCKER_IMAGE) $(NPM_REGISTRY_ARG) $(UCODE_SOURCE_ARG) $(PYPI_INDEX_ARG) docker/

# The docker-config* targets delegate to the same agent-* generation, only
# redirecting OUT_DIR to the container dir and layering the one override forced
# by the environment (the Linux OTEL helper path — the default is the macOS
# path) — so the harness tests exactly what the deploy targets produce. Pass
# other flags (e.g. --model-picker) via ARGS when you want to exercise them.
.PHONY: docker-config
docker-config: ## Generate Claude Code config for the container (Linux helper path); needs applied telemetry infra
	$(MAKE) agent-claude-code PROFILE=$(PROFILE) OUT_DIR=$(CONTAINER_CFG) \
		ARGS="--otel-helper-install-path /etc/claude-code/otel-headers-helper.sh $(ARGS)"

.PHONY: docker-config-codex
docker-config-codex: ## Generate Codex config.toml for the container (routes through the gateway mlflow/v1 responses route)
	$(MAKE) agent-codex PROFILE=$(PROFILE) OUT_DIR=$(CONTAINER_CFG) ARGS="$(ARGS)"

.PHONY: docker-config-all
docker-config-all: docker-config docker-config-codex ## Generate both agent configs for the container

.PHONY: docker-reload
docker-reload: docker-config-all ## Regenerate BOTH agent configs and copy them into the RUNNING container (no restart, keeps auth)
	docker cp $(CONTAINER_CFG)/claude-code/managed-settings.json $(DOCKER_CONTAINER):/etc/claude-code/managed-settings.json
	@if [ -f "$(CONTAINER_CFG)/claude-code/otel-headers-helper.sh" ]; then \
		docker cp $(CONTAINER_CFG)/claude-code/otel-headers-helper.sh $(DOCKER_CONTAINER):/etc/claude-code/otel-headers-helper.sh; \
		docker exec -u root $(DOCKER_CONTAINER) chmod +x /etc/claude-code/otel-headers-helper.sh; \
	fi
	docker cp $(CONTAINER_CFG)/codex/config.toml $(DOCKER_CONTAINER):/home/dev/.codex/config.toml
	docker exec -u root $(DOCKER_CONTAINER) chown dev:dev /home/dev/.codex/config.toml
	@echo "Harness reloaded (Claude Code + Codex). Restart your \`claude\` / \`codex\` session (exit and re-run) to pick it up."

.PHONY: docker-up
docker-up: ## Start the container (mounts configs, maps OAuth port 8020, writes the profile)
	@test -f "$(CONTAINER_CFG)/claude-code/managed-settings.json" -o -f "$(CONTAINER_CFG)/codex/config.toml" \
		|| { echo "No config in $(CONTAINER_CFG)/ — run 'make docker-config' and/or 'make docker-config-codex' first."; exit 1; }
	@test -n "$(WS_HOST)" \
		|| { echo "Could not resolve host for profile '$(PROFILE)' in ~/.databrickscfg."; exit 1; }
	docker run -d --name $(DOCKER_CONTAINER) \
		-p 8020:8020 \
		-e DATABRICKS_WS_HOST="$(WS_HOST)" \
		-e DATABRICKS_PROFILE_NAME="$(PROFILE)" \
		-e DATABRICKS_CONFIG_PROFILE="$(PROFILE)" \
		-v "$(abspath $(CONTAINER_CFG)/claude-code)":/opt/agent-config:ro \
		$(CODEX_CFG_MOUNT) \
		$(DOCKER_IMAGE)
	@echo ""
	@echo "Container '$(DOCKER_CONTAINER)' up (profile '$(PROFILE)' -> $(WS_HOST))."
	@echo "  make docker-login   # authenticate (opens a URL to paste into your host browser)"
	@echo "  make docker-shell   # then run: claude   (or: codex / ucode codex)"

.PHONY: docker-login
docker-login: ## Run `databricks auth login` inside the container (default profile)
	docker exec -it -u dev $(DOCKER_CONTAINER) databricks auth login --profile $(PROFILE)

.PHONY: docker-mcp
docker-mcp: ## Discover + register Databricks MCP servers into Claude Code's user config (runs `ucode configure mcp` inside; auth first via docker-login)
	docker exec -it -u dev -w /home/dev/work \
		-e DATABRICKS_CONFIG_PROFILE="$(PROFILE)" \
		$(DOCKER_CONTAINER) ucode configure mcp $(ARGS)

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
docker-test: docker-build docker-config-all docker-up ## Build + generate both agent configs + up in one step
