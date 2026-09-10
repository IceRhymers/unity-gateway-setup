# unity-gateway-setup — main task executor
#
# Run `make` or `make help` to list targets.

# ---- configuration (override on the command line, e.g. `make tf-plan TF_DIR=...`) ----
TF        ?= terraform
TF_ROOT   ?= terraform
TF_DIR    ?= terraform/infra
ARGS      ?=

PYTHON    ?= python3
# The one profile name this repo targets on every machine. The NAME is fixed so a
# local test and an MDM rollout share one auth path; the WORKSPACE is not, because
# the host is baked into each artifact at generation time. Override for a one-off.
PROFILE   ?= ai_dev_tools
# Workspace URL used only to make the setup hint copy-pasteable when the profile
# is missing. Never baked into anything.
HOST      ?=
# Export PROFILE_EXPLICIT=1 only when PROFILE was set on the command line or
# in the environment — never when it came from the default above. with-state.sh
# uses this flag to skip the profile-mismatch check for the Makefile default,
# so operators with a different profile name are not blocked on every tf-* call.
ifeq ($(origin PROFILE),command line)
export PROFILE_EXPLICIT := 1
else ifeq ($(origin PROFILE),environment)
export PROFILE_EXPLICIT := 1
endif
# Wrapper that injects Lakebase state credentials. State-touching targets
# route through it; it is a no-op passthrough when this checkout is not
# wired to the remote backend.
TF_WRAP   ?= terraform/bootstrap/with-state.sh
# Every generator invocation READS Terraform outputs, so it goes through the
# wrapper that injects the Lakebase state credentials. TF_STATE_DIR is required
# here: the generator runs `terraform -chdir=$(TF_DIR) output -json` from inside
# Python, so the wrapper sees no -chdir= in its own argv and cannot infer the
# directory. Without it the wrapper passes through with no credentials and the
# pg backend dials libpq's default 127.0.0.1:5432.
AGENT_GEN ?= TF_STATE_DIR=$(TF_DIR) $(TF_WRAP) $(PYTHON) agent_setups/scripts/generate.py
# Share one provider download between .terraform and .terraform.validate.
export TF_PLUGIN_CACHE_DIR ?= $(HOME)/.terraform.d/plugin-cache
# Where generated configs land. The docker-config* targets override this to the
# container dir so the harness tests exactly what `agent-*` generates.
OUT_DIR   ?= agent_setups/generated

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

# ---- profile preflight ----
# Every target below that authenticates against a workspace takes ensure-profile-quiet
# as an ORDER-ONLY prerequisite (after the `|`), so a missing profile fails on the
# first command with the login line to run, instead of three steps in.
#
# The check asks the databricks CLI, never ~/.databrickscfg, because that file holds
# tokens and client secrets for every workspace an operator has. See ensure-profile.sh.
_ENSURE_PROFILE := sh agent_setups/deploy/ensure-profile.sh --profile $(PROFILE) $(if $(HOST),--host $(HOST),)

.PHONY: ensure-profile
ensure-profile: ## Verify the ai_dev_tools profile exists and authenticates, and print the login command if not (PROFILE=, HOST=)
	$(_ENSURE_PROFILE) --validate

.PHONY: ensure-profile-quiet
ensure-profile-quiet:
	@$(_ENSURE_PROFILE) --quiet

# The mutating counterpart. Deliberately NOT gated by ensure-profile-quiet: this is
# how an operator satisfies that gate. It also refreshes an expired session, which is
# what ensure-profile reports as exit 5.
.PHONY: login-profile
login-profile: ## Create or refresh the ai_dev_tools profile with browser SSO (HOST=<workspace-url> required; PROFILE=, ARGS=)
	sh agent_setups/deploy/login-profile.sh --profile $(PROFILE) --host "$(HOST)" $(ARGS)

# ---- includes ----
# Split by domain so a change to one subsystem does not scroll past the others.
# Listed explicitly rather than globbed, so the set is reviewable and the read
# order is fixed. Variables above are defined before this point on purpose:
# an included file may use them, but must not need to redefine them.
include make/terraform.mk
include make/agents.mk
include make/tests.mk
include make/packages.mk
include make/docker.mk
