# packages.mk — Release artifacts: the macOS installer packages and the per-OS deploy tarballs.
#
# Included from the root Makefile, which defines every variable used here
# (PROFILE, TF_WRAP, AGENT_GEN, OUT_DIR, VERSION, ...). Do not run this file
# directly. `make help` lists these targets: it greps $$(MAKEFILE_LIST), so an
# included target's `## comment` shows up with no extra wiring.

# ---- deployment packaging ----

# ---- macOS installer packages ----
# A .mobileconfig carries SETTINGS ONLY: it cannot place a file or run a command.
# So every managed file needs a .pkg, which is also the artifact every MDM deploys.
#
# Three packages, versioned independently, so one piece can be staged or rolled back
# without touching the others:
#   coding-agents-pkg   -> Claude Code + Codex managed configs
#   claude-desktop-pkg  -> Claude Desktop helper scripts
#   ug-bootstrap-pkg    -> the SSO bootstrap + its LaunchAgent
#
# ug-bootstrap-pkg packages neither ug nor uv. Installing ug is per-user, and a
# package script runs as root, so the install is deferred to first login where the
# LaunchAgent runs it in the user's own session. uv is a prerequisite that IT owns,
# because it also installs per-user and self-updates.
#
# PKG_SIGN_ID signs a package for distribution.
PKG_SIGN_ID ?=
PKG_VERSION ?= $(VERSION)

.PHONY: ug-bootstrap-pkg
ug-bootstrap-pkg: | ensure-profile-quiet ## Build the macOS .pkg that places the ug SSO bootstrap + its LaunchAgent (PROFILE=, PKG_SIGN_ID=, ARGS=)
	sh agent_setups/deploy/build-ug-bootstrap-pkg.sh \
		--source "$(OUT_DIR)/claude-desktop/macos" \
		--out "$(DIST_DIR)/ug-bootstrap-$(PKG_VERSION).pkg" \
		--version "$(PKG_VERSION)" \
		$(if $(PKG_SIGN_ID),--sign "$(PKG_SIGN_ID)",) $(ARGS)

.PHONY: coding-agents-pkg
coding-agents-pkg: | ensure-profile-quiet ## Build the macOS .pkg that places the Claude Code + Codex managed configs (PROFILE=, PKG_SIGN_ID=, ARGS=)
	sh agent_setups/deploy/build-coding-agents-pkg.sh \
		--source "$(OUT_DIR)" \
		--out "$(DIST_DIR)/coding-agents-$(PKG_VERSION).pkg" \
		--version "$(PKG_VERSION)" \
		$(if $(PKG_SIGN_ID),--sign "$(PKG_SIGN_ID)",) $(ARGS)

.PHONY: claude-desktop-pkg
claude-desktop-pkg: | ensure-profile-quiet ## Build the macOS .pkg that places the Claude Desktop helper scripts (PROFILE=, PKG_SIGN_ID=, ARGS=)
	sh agent_setups/deploy/build-claude-desktop-pkg.sh \
		--source "$(OUT_DIR)/claude-desktop/macos" \
		--out "$(DIST_DIR)/claude-desktop-$(PKG_VERSION).pkg" \
		--version "$(PKG_VERSION)" \
		$(if $(PKG_SIGN_ID),--sign "$(PKG_SIGN_ID)",) $(ARGS)

.PHONY: packages
packages: coding-agents-pkg claude-desktop-pkg ug-bootstrap-pkg | ensure-profile-quiet ## Build every macOS installer package (generate the bundles first; PROFILE=, PKG_SIGN_ID=, ARGS=)
	@echo ""
	@echo "[packages] Built in $(DIST_DIR)/:"
	@ls -1 "$(DIST_DIR)"/*-$(PKG_VERSION).pkg 2>/dev/null | sed 's/^/  /'
	@echo ""
	@echo "[packages] Install order on a device: any. They share no file."
	@echo "[packages] Still needed, and NOT built here:"
	@echo "  - the .mobileconfig, which the Claude Desktop app exports after an import"
	@echo "  - uv on the device, a prerequisite the bootstrap installs ug with"

.PHONY: deploy-package
deploy-package: | ensure-profile-quiet ## Build self-contained per-OS deploy tarballs in dist/ (generate the bundles first)
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

