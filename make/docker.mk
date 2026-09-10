# docker.mk — The container test harness: build, config staging, lifecycle, and shell access.
#
# Included from the root Makefile, which defines every variable used here
# (PROFILE, TF_WRAP, AGENT_GEN, OUT_DIR, VERSION, ...). Do not run this file
# directly. `make help` lists these targets: it greps $$(MAKEFILE_LIST), so an
# included target's `## comment` shows up with no extra wiring.

# ---- docker test harness ----
# Isolated container to test the generated agent configs (Claude Code routing +
# OTEL telemetry, Codex gateway routing, and DeepSeek Harness gateway routing)
# without touching the host's own settings. Typical flow:
#   make tf-apply                 # provision the telemetry infra (once)
#   make docker-build             # build the image (once)
#   make docker-config-all        # generate all agent configs (or -config / -config-codex / -config-dsh)
#   make docker-up                # start the container
#   make docker-login             # databricks auth login inside (browser on host)
#   make docker-shell             # exec in; run `claude`, `codex`, or `dsh` to generate traffic

DOCKER_IMAGE     ?= unity-gateway-test
DOCKER_CONTAINER ?= unity-gateway-test
CONTAINER_CFG    ?= agent_setups/generated/container
# Mount the Codex config only when it has been generated (docker-config-codex),
# so the harness works with either or both agents present.
CODEX_CFG_MOUNT   = $(if $(or $(wildcard $(CONTAINER_CFG)/codex/config.toml),$(wildcard $(CONTAINER_CFG)/codex/etc/managed_config.toml)),-v "$(abspath $(CONTAINER_CFG)/codex)":/opt/agent-config-codex:ro,)
# Mount the DeepSeek Harness config only when it has been generated
# (docker-config-dsh). The entrypoint stages it into the dev user's ~/.dsh.
DSH_CFG_MOUNT     = $(if $(wildcard $(CONTAINER_CFG)/dsh/cordis.patch.yml),-v "$(abspath $(CONTAINER_CFG)/dsh)":/opt/agent-config-dsh:ro,)
# Workspace host for PROFILE. Asked of the databricks CLI, not parsed out of
# ~/.databrickscfg: that file holds tokens and client secrets, and this only needs
# the host. `auth profiles` reports name/host/cloud/auth_type/valid and no secret.
# --skip-validate keeps it offline, because this runs at every parse.
WS_HOST := $(shell databricks auth profiles -o json --skip-validate 2>/dev/null | jq -r --arg p '$(PROFILE)' '.profiles[]? | select(.name == $$p) | .host // empty' 2>/dev/null)
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
docker-build: ## Build the test-harness image (Claude Code + Codex + dsh + databricks CLI + python3 + ug)
	docker build -t $(DOCKER_IMAGE) $(NPM_REGISTRY_ARG) $(UCODE_SOURCE_ARG) $(PYPI_INDEX_ARG) -f docker/Dockerfile .

# The docker-config* targets delegate to the SAME agent-* generation, only
# redirecting OUT_DIR to the container dir — no docker-specific overrides. The
# generator emits a per-OS bundle (claude-code/{macos,linux,windows}/); the harness
# simply mounts the linux/ one, so it tests exactly what the deploy targets produce.
# Pass other flags (e.g. --model-picker) via ARGS when you want to exercise them.
.PHONY: docker-config
docker-config: | ensure-profile-quiet ## Generate Claude Code config bundles for the container; needs applied telemetry infra
	$(MAKE) agent-claude-code PROFILE=$(PROFILE) OUT_DIR=$(CONTAINER_CFG) ARGS="$(ARGS)"

.PHONY: docker-config-codex
docker-config-codex: | ensure-profile-quiet ## Generate Codex config.toml for the container (routes through the gateway mlflow/v1 responses route)
	$(MAKE) agent-codex PROFILE=$(PROFILE) OUT_DIR=$(CONTAINER_CFG) ARGS="$(ARGS)"

.PHONY: docker-config-dsh
docker-config-dsh: | ensure-profile-quiet ## Generate the DeepSeek Harness home patch + token plugin for the container (routes the DeepSeek adapter through the gateway mlflow/v1 route)
	$(MAKE) agent-dsh PROFILE=$(PROFILE) OUT_DIR=$(CONTAINER_CFG) ARGS="$(ARGS)"

.PHONY: docker-config-all
docker-config-all: docker-config docker-config-codex docker-config-dsh | ensure-profile-quiet ## Generate every agent config for the container (claude-code + codex + dsh)

.PHONY: docker-reload
docker-reload: docker-config-all | ensure-profile-quiet ## Regenerate BOTH agent configs and hot-reload into the RUNNING container via install.sh (no restart)
	@# Create a writable temp staging area inside the container.
	@# The /opt/agent-config* mounts are :ro so we cannot write there directly.
	docker exec -u root $(DOCKER_CONTAINER) sh -c \
	  'rm -rf /tmp/ugw-reload && mkdir -p /tmp/ugw-reload'
	@# Push the current install.sh into the container.
	docker cp $(INSTALL_SH) $(DOCKER_CONTAINER):/tmp/ugw-reload/install.sh
	docker exec -u root $(DOCKER_CONTAINER) chmod +x /tmp/ugw-reload/install.sh
	@# Copy each config bundle to the writable temp area, then delegate ALL
	@# placement decisions (paths, perms, owner) exclusively to install.sh.
	@# AC7 hard gate: no independent copy/chmod matrix in this recipe.
	@set -e; \
	_agents=""; \
	_staged=""; \
	if [ -f "$(CONTAINER_CFG)/claude-code/linux/managed-settings.json" ]; then \
	  docker exec -u root $(DOCKER_CONTAINER) mkdir -p /tmp/ugw-reload/claude; \
	  docker cp "$(CONTAINER_CFG)/claude-code/linux/." $(DOCKER_CONTAINER):/tmp/ugw-reload/claude/; \
	  _agents="claude-code"; \
	fi; \
	if [ -f "$(CONTAINER_CFG)/codex/etc/managed_config.toml" ]; then \
	  docker exec -u root $(DOCKER_CONTAINER) mkdir -p /tmp/ugw-reload/codex; \
	  docker cp "$(CONTAINER_CFG)/codex/." $(DOCKER_CONTAINER):/tmp/ugw-reload/codex/; \
	  _agents="$${_agents:+$${_agents},}codex"; \
	fi; \
	if [ -n "$${_agents}" ]; then \
	  docker exec -u root $(DOCKER_CONTAINER) /tmp/ugw-reload/install.sh \
	    --agents "$${_agents}" \
	    --claude-source /tmp/ugw-reload/claude \
	    --codex-source /tmp/ugw-reload/codex \
	    --os linux; \
	  _staged="$${_agents}"; \
	fi; \
	if [ -f "$(CONTAINER_CFG)/dsh/cordis.patch.yml" ]; then \
	  docker exec -u root $(DOCKER_CONTAINER) mkdir -p /tmp/ugw-reload/dsh; \
	  docker cp "$(CONTAINER_CFG)/dsh/." $(DOCKER_CONTAINER):/tmp/ugw-reload/dsh/; \
	  docker exec -u root $(DOCKER_CONTAINER) chown -R dev:dev /tmp/ugw-reload/dsh; \
	  docker exec -u dev $(DOCKER_CONTAINER) /usr/local/lib/unity-gateway/install-dsh-local.sh \
	    --source /tmp/ugw-reload/dsh/cordis.patch.yml \
	    --target-dir /home/dev/.dsh \
	    --no-backup; \
	  _staged="$${_staged:+$${_staged},}dsh"; \
	fi; \
	if [ -z "$${_staged}" ]; then \
	  echo "[docker-reload] No configs found in $(CONTAINER_CFG). Run make docker-config-all first."; \
	fi
	@docker exec -u root $(DOCKER_CONTAINER) rm -rf /tmp/ugw-reload
	@echo "Harness reloaded (Claude Code + Codex + DeepSeek Harness). Restart your \`claude\` / \`codex\` / \`dsh\` session (exit and re-run) to pick it up."

.PHONY: docker-up
docker-up: ## Start the container (mounts configs, maps OAuth port 8020, writes the profile)
	@test -f "$(CONTAINER_CFG)/claude-code/linux/managed-settings.json" -o -f "$(CONTAINER_CFG)/codex/config.toml" -o -f "$(CONTAINER_CFG)/codex/etc/managed_config.toml" -o -f "$(CONTAINER_CFG)/dsh/cordis.patch.yml" \
		|| { echo "No config in $(CONTAINER_CFG)/ — run 'make docker-config', 'make docker-config-codex', and/or 'make docker-config-dsh' first."; exit 1; }
	@test -n "$(WS_HOST)" \
		|| { echo "Could not resolve host for profile '$(PROFILE)' in ~/.databrickscfg."; exit 1; }
	docker run -d --name $(DOCKER_CONTAINER) \
		-p 8020:8020 \
		-e DATABRICKS_WS_HOST="$(WS_HOST)" \
		-e DATABRICKS_PROFILE_NAME="$(PROFILE)" \
		-e DATABRICKS_CONFIG_PROFILE="$(PROFILE)" \
		-v "$(abspath $(CONTAINER_CFG)/claude-code/linux)":/opt/agent-config:ro \
		$(CODEX_CFG_MOUNT) \
		$(DSH_CFG_MOUNT) \
		$(DOCKER_IMAGE)
	@echo ""
	@echo "Container '$(DOCKER_CONTAINER)' up (profile '$(PROFILE)' -> $(WS_HOST))."
	@echo "  make docker-login   # authenticate (opens a URL to paste into your host browser)"
	@echo "  make docker-shell   # then run: claude   (or: codex / ug codex / dsh web --no-open)"

.PHONY: docker-login
docker-login: ## Run `databricks auth login` inside the container (default profile)
	docker exec -it -u dev $(DOCKER_CONTAINER) databricks auth login --profile $(PROFILE)

.PHONY: docker-mcp
docker-mcp: ## Discover + register Databricks MCP servers into Claude Code's user config (runs `ug configure mcp` inside; auth first via docker-login)
	docker exec -it -u dev -w /home/dev/work \
		-e DATABRICKS_CONFIG_PROFILE="$(PROFILE)" \
		$(DOCKER_CONTAINER) ug configure mcp $(ARGS)

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
docker-test: docker-build docker-config-all docker-up ## Build + generate all agent configs (claude-code + codex + dsh) + up in one step
