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

# ---- mcp installer selection (override on the command line) ----
# CATALOG/SCHEMA scope the discovery (default: the system.ai managed MCP schema).
# Selection: SELECT=names (comma list) or ALL=1. Selection is declarative: the chosen
# set becomes the complete config for each harness. With neither SELECT nor ALL, the
# `mcp*` targets run interactively (an arrow-key menu).
CATALOG   ?= system
SCHEMA    ?= ai
SELECT    ?=
ALL       ?=
MCP_CAT_ARG := $(if $(CATALOG),--catalog $(CATALOG),)
MCP_SCH_ARG := $(if $(SCHEMA),--schema $(SCHEMA),)
MCP_SEL_ARG := $(if $(ALL),--all,$(if $(SELECT),--select $(SELECT),))

# Computed once so the tarball filename and embedded VERSION file are identical
# (no double git-describe drift). Format: <describe-or-sha>-<YYYYMMDD>.
VERSION   := $(shell git describe --tags --always 2>/dev/null || printf 'nogit')-$(shell date +%Y%m%d)
# Output directory for deploy-package tarballs.
DIST_DIR  ?= dist
# Path to the single placement installer, baked into the image and packaged.
INSTALL_SH := agent_setups/deploy/install.sh

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

.PHONY: agent-opencode
agent-opencode: ## Generate opencode.json from TF outputs (PROFILE=, OUT_DIR=, ARGS=)
	$(PYTHON) $(AGENT_GEN) opencode --profile $(PROFILE) --out-dir $(OUT_DIR) $(ARGS)

.PHONY: agent-opencode-preview
agent-opencode-preview: ## Print the generated opencode.json without writing
	$(PYTHON) $(AGENT_GEN) opencode --profile $(PROFILE) --stdout $(ARGS)

.PHONY: agent-dsh
agent-dsh: ## Generate the DeepSeek Harness home patch + token plugin from TF outputs (PROFILE=, OUT_DIR=, ARGS=)
	$(PYTHON) $(AGENT_GEN) dsh --profile $(PROFILE) --out-dir $(OUT_DIR) $(ARGS)

.PHONY: agent-dsh-preview
agent-dsh-preview: ## Print the generated DeepSeek Harness patch + plugin without writing
	$(PYTHON) $(AGENT_GEN) dsh --profile $(PROFILE) --stdout $(ARGS)

.PHONY: agent-claude-desktop
agent-claude-desktop: ## Generate the importable Claude Desktop config + helper scripts from TF outputs (PROFILE=, OUT_DIR=, ARGS=)
	$(PYTHON) $(AGENT_GEN) claude-desktop --profile $(PROFILE) --out-dir $(OUT_DIR) $(ARGS)

.PHONY: agent-claude-desktop-preview
agent-claude-desktop-preview: ## Print the generated Claude Desktop bundle without writing
	$(PYTHON) $(AGENT_GEN) claude-desktop --profile $(PROFILE) --stdout $(ARGS)

.PHONY: agents
agents: agent-claude-code agent-claude-desktop agent-codex agent-opencode agent-dsh ## Generate every agent config (claude-code + claude-desktop + codex + opencode + dsh)

.PHONY: opencode-install-local
opencode-install-local: ## Generate opencode.json (user mode) + install it to ~/.config/opencode for a local, non-managed install (PROFILE=, ARGS=)
	$(PYTHON) $(AGENT_GEN) opencode --user-config --profile $(PROFILE) --out-dir $(OUT_DIR) $(ARGS)
	sh agent_setups/deploy/install-opencode-local.sh --source $(OUT_DIR)/opencode/opencode.json

.PHONY: claude-code-install-local
claude-code-install-local: ## Generate settings.json (user mode) + install it to ~/.claude for a local, non-managed install (PROFILE=, ARGS=)
	$(PYTHON) $(AGENT_GEN) claude-code --user-config --profile $(PROFILE) --out-dir $(OUT_DIR) $(ARGS)
	sh agent_setups/deploy/install-claude-code-local.sh --source $(OUT_DIR)/claude-code/user/settings.json

.PHONY: codex-install-local
codex-install-local: ## Generate config.toml (user mode) + install it to ~/.codex for a local, non-managed install (PROFILE=, ARGS=)
	$(PYTHON) $(AGENT_GEN) codex --user-config --profile $(PROFILE) --out-dir $(OUT_DIR) $(ARGS)
	sh agent_setups/deploy/install-codex-local.sh --source $(OUT_DIR)/codex/config.toml

.PHONY: dsh-install-local
dsh-install-local: ## Generate the DeepSeek Harness patch + plugin + install them to $DSH_HOME (default ~/.dsh) for a local install (PROFILE=, ARGS=)
	$(PYTHON) $(AGENT_GEN) dsh --profile $(PROFILE) --out-dir $(OUT_DIR) $(ARGS)
	sh agent_setups/deploy/install-dsh-local.sh --source $(OUT_DIR)/dsh/cordis.patch.yml

# ---- claude-desktop local test install ----
# Claude Desktop reads an operator-imported config, so there is no config file to
# place. Only the helper scripts the JSON references need to exist on disk. This
# target generates a bundle for THIS OS with the helper path set to a user-writable
# dir, then places the helper scripts there — so you can import claude-setup.json in
# the app and test it without root. Override CD_LOCAL_DIR / CD_OS as needed.
CD_UNAME_S := $(shell uname -s)
CD_OS ?= $(if $(filter Darwin,$(CD_UNAME_S)),macos,linux)
CD_LOCAL_DIR ?= $(if $(filter macos,$(CD_OS)),$(HOME)/Library/Application Support/ClaudeDesktop,$(HOME)/.config/claude-desktop)

.PHONY: claude-desktop-install-local
claude-desktop-install-local: ## Generate a Claude Desktop bundle pointed at a user dir + place its helper scripts there for local testing (PROFILE=, CD_LOCAL_DIR=, CD_OS=, ARGS=)
	$(PYTHON) $(AGENT_GEN) claude-desktop --profile $(PROFILE) --out-dir $(OUT_DIR) \
		--platforms $(CD_OS) --install-dir-$(CD_OS) "$(CD_LOCAL_DIR)" $(ARGS)
	sh agent_setups/deploy/install-claude-desktop-local.sh \
		--source "$(OUT_DIR)/claude-desktop/$(CD_OS)" --target-dir "$(CD_LOCAL_DIR)"

.PHONY: agents-install-local
agents-install-local: claude-code-install-local codex-install-local opencode-install-local dsh-install-local ## Install ALL agent configs locally (user mode) to their per-user dirs, backing up existing files (PROFILE=, ARGS=)
	@echo "[agents-install-local] Claude Code, Codex, opencode, and DeepSeek Harness installed locally."

# ---- mcp services ----
# Discover AI Gateway MCP services and merge selected ones into the harness USER
# configs. With neither ENABLE nor ALL set, the target runs interactively.

.PHONY: mcp
mcp: ## Select AI Gateway MCP services and install into ALL harnesses (PROFILE=, CATALOG=, SCHEMA=, SELECT=names, ALL=1, ARGS=)
	$(PYTHON) $(AGENT_GEN) mcp --profile $(PROFILE) $(MCP_CAT_ARG) $(MCP_SCH_ARG) $(MCP_SEL_ARG) $(ARGS)

.PHONY: mcp-claude-code
mcp-claude-code: ## Select AI Gateway MCP services and install into Claude Code only (same vars as `mcp`)
	$(PYTHON) $(AGENT_GEN) mcp --profile $(PROFILE) --harness claude-code $(MCP_CAT_ARG) $(MCP_SCH_ARG) $(MCP_SEL_ARG) $(ARGS)

.PHONY: mcp-codex
mcp-codex: ## Select AI Gateway MCP services and install into Codex only (same vars as `mcp`)
	$(PYTHON) $(AGENT_GEN) mcp --profile $(PROFILE) --harness codex $(MCP_CAT_ARG) $(MCP_SCH_ARG) $(MCP_SEL_ARG) $(ARGS)

.PHONY: mcp-opencode
mcp-opencode: ## Select AI Gateway MCP services and install into opencode only (same vars as `mcp`)
	$(PYTHON) $(AGENT_GEN) mcp --profile $(PROFILE) --harness opencode $(MCP_CAT_ARG) $(MCP_SCH_ARG) $(MCP_SEL_ARG) $(ARGS)

# ---- tests ----

.PHONY: test
test: ## Run the deploy install.sh test suite (self-contained: no infra, no network, no pre-generated bundles)
	sh agent_setups/deploy/tests/run.sh

.PHONY: test-mcp
test-mcp: ## Run the Python unit tests for the `mcp` installer (needs: pip install -r agent_setups/scripts/requirements.txt)
	$(PYTHON) -m unittest discover -s agent_setups/scripts/tests -v

.PHONY: check
check: tf-fmt-check tf-validate test test-mcp ## Run all static checks (tf-fmt-check + tf-validate + deploy tests + mcp unit tests; no creds needed)

# ---- deployment packaging ----

.PHONY: deploy-package
deploy-package: ## Build self-contained per-OS deploy tarballs in dist/ (generate the bundles first)
	@mkdir -p "$(DIST_DIR)"
	@echo "[deploy-package] VERSION=$(VERSION)"
	@# Fail loud on a mis-generated codex bundle: install.sh SILENTLY skips a user-mode
	@# codex (config.toml at root, no etc/managed_config.toml), which would ship a tarball
	@# whose Codex is quietly dropped at deploy time with a green exit. Require managed mode.
	@if [ -d "$(OUT_DIR)/codex" ] && [ ! -f "$(OUT_DIR)/codex/etc/managed_config.toml" ]; then \
	  echo "[deploy-package] ERROR: $(OUT_DIR)/codex is user-mode (no etc/managed_config.toml)."; \
	  echo "                 install.sh would skip it. Regenerate codex in managed mode:"; \
	  echo "                   make agent-codex OUT_DIR=$(OUT_DIR)"; \
	  exit 1; \
	fi
	@# Same guard for opencode: install.sh SILENTLY skips a user-mode opencode
	@# (opencode.json at root, no ai.opencode.managed.mobileconfig), which would ship
	@# a tarball whose opencode is quietly dropped at deploy time. Require managed mode.
	@if [ -d "$(OUT_DIR)/opencode" ] && [ ! -f "$(OUT_DIR)/opencode/ai.opencode.managed.mobileconfig" ]; then \
	  echo "[deploy-package] ERROR: $(OUT_DIR)/opencode is user-mode (no ai.opencode.managed.mobileconfig)."; \
	  echo "                 install.sh would skip it. Regenerate opencode in managed mode:"; \
	  echo "                   make agent-opencode OUT_DIR=$(OUT_DIR)"; \
	  exit 1; \
	fi
	@for os in macos linux; do \
	  tarball="$(DIST_DIR)/unity-gateway-agents-$(VERSION)-$${os}.tar.gz"; \
	  echo "[deploy-package] Building $${tarball} ..."; \
	  if [ ! -f "$(OUT_DIR)/claude-code/$${os}/managed-settings.json" ]; then \
	    echo "[deploy-package] ERROR: $(OUT_DIR)/claude-code/$${os}/managed-settings.json not found."; \
	    echo "                 Generate the bundle first: make agent-claude-code OUT_DIR=$(OUT_DIR)"; \
	    exit 1; \
	  fi; \
	  tmpdir="$$(mktemp -d)"; \
	  mkdir -p "$${tmpdir}/claude-code/$${os}"; \
	  cp -r "$(OUT_DIR)/claude-code/$${os}/." "$${tmpdir}/claude-code/$${os}/"; \
	  if [ -d "$(OUT_DIR)/codex" ]; then \
	    mkdir -p "$${tmpdir}/codex"; \
	    cp -r "$(OUT_DIR)/codex/." "$${tmpdir}/codex/"; \
	  fi; \
	  if [ -d "$(OUT_DIR)/opencode" ]; then \
	    mkdir -p "$${tmpdir}/opencode"; \
	    cp -r "$(OUT_DIR)/opencode/." "$${tmpdir}/opencode/"; \
	  fi; \
	  if [ -d "$(OUT_DIR)/claude-desktop/$${os}" ]; then \
	    mkdir -p "$${tmpdir}/claude-desktop/$${os}"; \
	    cp -r "$(OUT_DIR)/claude-desktop/$${os}/." "$${tmpdir}/claude-desktop/$${os}/"; \
	  fi; \
	  cp $(INSTALL_SH) "$${tmpdir}/install.sh"; \
	  printf '%s' "$(VERSION)" > "$${tmpdir}/VERSION"; \
	  if ls agent_setups/deploy/runbooks/*.md >/dev/null 2>&1; then \
	    cp agent_setups/deploy/runbooks/*.md "$${tmpdir}/" 2>/dev/null || true; \
	  fi; \
	  tar -czf "$${tarball}" -C "$${tmpdir}" .; \
	  rm -rf "$${tmpdir}"; \
	  echo "[deploy-package] Built $${tarball}"; \
	  _base="$$(basename "$${tarball}")"; \
	  if command -v sha256sum >/dev/null 2>&1; then \
	    ( cd "$(DIST_DIR)" && sha256sum "$${_base}" > "$${_base}.sha256" ); \
	  else \
	    ( cd "$(DIST_DIR)" && shasum -a 256 "$${_base}" > "$${_base}.sha256" ); \
	  fi; \
	  echo "[deploy-package] Wrote $${tarball}.sha256"; \
	done
	@echo "[deploy-package] Done. Tarballs in $(DIST_DIR)/"

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
# Mount the opencode config only when it has been generated (docker-config-opencode).
# Guard on the .mobileconfig — the managed-vs-user signal (both modes emit
# opencode.json; only managed mode emits the .mobileconfig). The entrypoint stages
# it into /etc/opencode/ (Linux managed path) inside the container.
OPENCODE_CFG_MOUNT = $(if $(wildcard $(CONTAINER_CFG)/opencode/ai.opencode.managed.mobileconfig),-v "$(abspath $(CONTAINER_CFG)/opencode)":/opt/agent-config-opencode:ro,)
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
docker-build: ## Build the test-harness image (Claude Code + Codex + opencode + dsh + databricks CLI + python3 + ug)
	docker build -t $(DOCKER_IMAGE) $(NPM_REGISTRY_ARG) $(UCODE_SOURCE_ARG) $(PYPI_INDEX_ARG) -f docker/Dockerfile .

# The docker-config* targets delegate to the SAME agent-* generation, only
# redirecting OUT_DIR to the container dir — no docker-specific overrides. The
# generator emits a per-OS bundle (claude-code/{macos,linux,windows}/); the harness
# simply mounts the linux/ one, so it tests exactly what the deploy targets produce.
# Pass other flags (e.g. --model-picker) via ARGS when you want to exercise them.
.PHONY: docker-config
docker-config: ## Generate Claude Code config bundles for the container; needs applied telemetry infra
	$(MAKE) agent-claude-code PROFILE=$(PROFILE) OUT_DIR=$(CONTAINER_CFG) ARGS="$(ARGS)"

.PHONY: docker-config-codex
docker-config-codex: ## Generate Codex config.toml for the container (routes through the gateway mlflow/v1 responses route)
	$(MAKE) agent-codex PROFILE=$(PROFILE) OUT_DIR=$(CONTAINER_CFG) ARGS="$(ARGS)"

.PHONY: docker-config-dsh
docker-config-dsh: ## Generate the DeepSeek Harness home patch + token plugin for the container (routes the DeepSeek adapter through the gateway mlflow/v1 route)
	$(MAKE) agent-dsh PROFILE=$(PROFILE) OUT_DIR=$(CONTAINER_CFG) ARGS="$(ARGS)"

.PHONY: docker-config-opencode
docker-config-opencode: ## Generate opencode config for the container (routes through the gateway; stages Linux managed at /etc/opencode/)
	@if echo "$(ARGS)" | grep -q -- '--user-config'; then \
	  echo "ERROR: docker-config-opencode does not support --user-config. The container harness requires managed opencode only. Remove --user-config and run again."; \
	  exit 1; \
	fi
	$(MAKE) agent-opencode PROFILE=$(PROFILE) OUT_DIR=$(CONTAINER_CFG) ARGS="$(ARGS)"

.PHONY: docker-config-all
docker-config-all: docker-config docker-config-codex docker-config-opencode docker-config-dsh ## Generate every agent config for the container (claude-code + codex + opencode + dsh)

.PHONY: docker-reload
docker-reload: docker-config-all ## Regenerate BOTH agent configs and hot-reload into the RUNNING container via install.sh (no restart)
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
	if [ -f "$(CONTAINER_CFG)/opencode/ai.opencode.managed.mobileconfig" ]; then \
	  docker exec -u root $(DOCKER_CONTAINER) mkdir -p /tmp/ugw-reload/opencode; \
	  docker cp "$(CONTAINER_CFG)/opencode/." $(DOCKER_CONTAINER):/tmp/ugw-reload/opencode/; \
	  _agents="$${_agents:+$${_agents},}opencode"; \
	fi; \
	if [ -n "$${_agents}" ]; then \
	  docker exec -u root $(DOCKER_CONTAINER) /tmp/ugw-reload/install.sh \
	    --agents "$${_agents}" \
	    --claude-source /tmp/ugw-reload/claude \
	    --codex-source /tmp/ugw-reload/codex \
	    --opencode-source /tmp/ugw-reload/opencode \
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
	@echo "Harness reloaded (Claude Code + Codex + opencode + DeepSeek Harness). Restart your \`claude\` / \`codex\` / \`opencode\` / \`dsh\` session (exit and re-run) to pick it up."

.PHONY: docker-up
docker-up: ## Start the container (mounts configs, maps OAuth port 8020, writes the profile)
	@test -f "$(CONTAINER_CFG)/claude-code/linux/managed-settings.json" -o -f "$(CONTAINER_CFG)/codex/config.toml" -o -f "$(CONTAINER_CFG)/codex/etc/managed_config.toml" -o -f "$(CONTAINER_CFG)/opencode/ai.opencode.managed.mobileconfig" -o -f "$(CONTAINER_CFG)/dsh/cordis.patch.yml" \
		|| { echo "No config in $(CONTAINER_CFG)/ — run 'make docker-config', 'make docker-config-codex', 'make docker-config-opencode', and/or 'make docker-config-dsh' first."; exit 1; }
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
		$(OPENCODE_CFG_MOUNT) \
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
