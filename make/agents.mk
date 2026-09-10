# agents.mk — Agent config generation, and the local + fleet-path test installs.
#
# Included from the root Makefile, which defines every variable used here
# (PROFILE, TF_WRAP, AGENT_GEN, OUT_DIR, VERSION, ...). Do not run this file
# directly. `make help` lists these targets: it greps $$(MAKEFILE_LIST), so an
# included target's `## comment` shows up with no extra wiring.

# ---- agent configs ----

.PHONY: agent-claude-code
agent-claude-code: | ensure-profile-quiet ## Generate Claude Code managed-settings.json from TF outputs (PROFILE=, OUT_DIR=, ARGS=)
	$(AGENT_GEN) claude-code --profile $(PROFILE) --out-dir $(OUT_DIR) $(ARGS)

.PHONY: agent-claude-code-preview
agent-claude-code-preview: | ensure-profile-quiet ## Print the generated Claude Code managed-settings.json without writing
	$(AGENT_GEN) claude-code --profile $(PROFILE) --stdout $(ARGS)

.PHONY: agent-codex
agent-codex: | ensure-profile-quiet ## Generate Codex config.toml from TF outputs (PROFILE=, OUT_DIR=, ARGS=)
	$(AGENT_GEN) codex --profile $(PROFILE) --out-dir $(OUT_DIR) $(ARGS)

.PHONY: agent-codex-preview
agent-codex-preview: | ensure-profile-quiet ## Print the generated Codex config.toml without writing
	$(AGENT_GEN) codex --profile $(PROFILE) --stdout $(ARGS)

.PHONY: agent-dsh
agent-dsh: | ensure-profile-quiet ## Generate the DeepSeek Harness home patch + token plugin from TF outputs (PROFILE=, OUT_DIR=, ARGS=)
	$(AGENT_GEN) dsh --profile $(PROFILE) --out-dir $(OUT_DIR) $(ARGS)

.PHONY: agent-dsh-preview
agent-dsh-preview: | ensure-profile-quiet ## Print the generated DeepSeek Harness patch + plugin without writing
	$(AGENT_GEN) dsh --profile $(PROFILE) --stdout $(ARGS)

.PHONY: agent-claude-desktop
agent-claude-desktop: | ensure-profile-quiet ## Generate the importable Claude Desktop config + helper scripts from TF outputs (PROFILE=, OUT_DIR=, ARGS=)
	$(AGENT_GEN) claude-desktop --profile $(PROFILE) --out-dir $(OUT_DIR) $(ARGS)

.PHONY: agent-claude-desktop-preview
agent-claude-desktop-preview: | ensure-profile-quiet ## Print the generated Claude Desktop bundle without writing
	$(AGENT_GEN) claude-desktop --profile $(PROFILE) --stdout $(ARGS)

.PHONY: agents
agents: agent-claude-code agent-claude-desktop agent-codex agent-dsh | ensure-profile-quiet ## Generate every agent config (claude-code + claude-desktop + codex + dsh)

.PHONY: claude-code-install-local
claude-code-install-local: | ensure-profile-quiet ## Generate settings.json (user mode) + install it to ~/.claude for a local, non-managed install (PROFILE=, ARGS=)
	$(AGENT_GEN) claude-code --user-config --profile $(PROFILE) --out-dir $(OUT_DIR) $(ARGS)
	sh agent_setups/deploy/install-claude-code-local.sh --source $(OUT_DIR)/claude-code/user/settings.json

.PHONY: codex-install-local
codex-install-local: | ensure-profile-quiet ## Generate config.toml (user mode) + install it to ~/.codex for a local, non-managed install (PROFILE=, ARGS=)
	$(AGENT_GEN) codex --user-config --profile $(PROFILE) --out-dir $(OUT_DIR) $(ARGS)
	sh agent_setups/deploy/install-codex-local.sh --source $(OUT_DIR)/codex/config.toml

.PHONY: dsh-install-local
dsh-install-local: | ensure-profile-quiet ## Generate the DeepSeek Harness patch + plugin + install them to $DSH_HOME (default ~/.dsh) for a local install (PROFILE=, ARGS=)
	$(AGENT_GEN) dsh --profile $(PROFILE) --out-dir $(OUT_DIR) $(ARGS)
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
claude-desktop-install-local: | ensure-profile-quiet ## Generate a Claude Desktop bundle pointed at a user dir + place its helper scripts there for local testing (PROFILE=, CD_LOCAL_DIR=, CD_OS=, ARGS=)
	$(AGENT_GEN) claude-desktop --profile $(PROFILE) --out-dir $(OUT_DIR) \
		--platforms $(CD_OS) --install-dir-$(CD_OS) "$(CD_LOCAL_DIR)" $(ARGS)
	sh agent_setups/deploy/install-claude-desktop-local.sh \
		--source "$(OUT_DIR)/claude-desktop/$(CD_OS)" --target-dir "$(CD_LOCAL_DIR)"

# ---- claude-desktop system (fleet-path) test install ----
# The MDM path bakes a MACHINE-WIDE helper dir into claude-setup.json, so the
# user-dir target above cannot exercise the config an MDM would actually push.
# This target generates a bundle for THIS OS at the DEFAULT (fleet) helper dir,
# then places the helpers there through install.sh — the same placement authority
# the package path uses, so the two cannot drift apart. Both sides already agree:
# macOS "/Library/Application Support/ClaudeDesktop", Linux "/etc/claude-desktop".
#
# On macOS this also places the SSO-bootstrap LaunchAgent in /Library/LaunchAgents,
# because install.sh treats it as part of the claude-desktop bundle. That is what
# you want here: it is the same pair an MDM pushes.
#
# The target dir is root-owned, so this calls sudo. Two rehearsal modes:
#   make claude-desktop-install-system CD_INSTALL_ARGS=--dry-run
#   make claude-desktop-install-system CD_SUDO= CD_INSTALL_ARGS='--target-root /tmp/cd-stage'
# install.sh checks for root BEFORE it reads --dry-run, so a plain dry run still
# needs sudo. --target-root stages into a prefix unprivileged instead.
CD_SUDO ?= sudo
CD_INSTALL_ARGS ?=

.PHONY: claude-desktop-install-system
claude-desktop-install-system: | ensure-profile-quiet ## Generate a Claude Desktop bundle at the FLEET helper path + place the helpers there via install.sh (needs root; PROFILE=, CD_OS=, CD_SUDO=, CD_INSTALL_ARGS=, ARGS=)
	$(AGENT_GEN) claude-desktop --profile $(PROFILE) --out-dir $(OUT_DIR) \
		--platforms $(CD_OS) $(ARGS)
	$(CD_SUDO) sh agent_setups/deploy/install.sh \
		--agents claude-desktop --os $(CD_OS) --source "$(OUT_DIR)" $(CD_INSTALL_ARGS)
	@printf '[claude-desktop-install-system] helpers: %s\n' \
		"$$(sh agent_setups/deploy/install.sh --os $(CD_OS) --agent claude-desktop --print-target-dir)"
	@printf '[claude-desktop-install-system] import in the app (Developer -> Configure): %s\n' \
		"$(OUT_DIR)/claude-desktop/$(CD_OS)/claude-setup.json"

.PHONY: agents-install-local
agents-install-local: claude-code-install-local codex-install-local dsh-install-local | ensure-profile-quiet ## Install ALL agent configs locally (user mode) to their per-user dirs, backing up existing files (PROFILE=, ARGS=)
	@echo "[agents-install-local] Claude Code, Codex, and DeepSeek Harness installed locally."

