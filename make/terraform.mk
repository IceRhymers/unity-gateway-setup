# terraform.mk — Terraform: fmt, validate, plan/apply/destroy, outputs, and Lakebase remote state.
#
# Included from the root Makefile, which defines every variable used here
# (PROFILE, TF_WRAP, AGENT_GEN, OUT_DIR, VERSION, ...). Do not run this file
# directly. `make help` lists these targets: it greps $$(MAKEFILE_LIST), so an
# included target's `## comment` shows up with no extra wiring.

# ---- terraform ----

.PHONY: tf-fmt
tf-fmt: ## Format all Terraform files in place
	$(TF) fmt -recursive $(TF_ROOT)

.PHONY: tf-fmt-check
tf-fmt-check: ## Check Terraform formatting (no writes; non-zero if unformatted)
	$(TF) fmt -recursive -check -diff $(TF_ROOT)

.PHONY: tf-init
tf-init: | ensure-profile-quiet ## Initialize the infra working directory (downloads providers, configures backend)
	$(TF_WRAP) $(TF) -chdir=$(TF_DIR) init -input=false $(ARGS)

# `init -backend=false` still LOADS an initialized backend, and the pg
# backend dials Postgres eagerly - so once tf-init has run, validate would
# fail offline. A separate data dir has no backend record, so nothing dials.
# Do not remove this: it is load-bearing, not cosmetic.
.PHONY: tf-validate
tf-validate: export TF_DATA_DIR := .terraform.validate
tf-validate: ## Validate the infra configuration (no credentials/network required)
	$(TF) -chdir=$(TF_DIR) init -backend=false -input=false >/dev/null
	$(TF) -chdir=$(TF_DIR) validate

.PHONY: tf-plan
tf-plan: | ensure-profile-quiet ## Show the execution plan against the target workspace (read-only)
	$(TF_WRAP) $(TF) -chdir=$(TF_DIR) plan -input=false $(ARGS)

.PHONY: tf-apply
tf-apply: | ensure-profile-quiet ## Apply the configuration (prompts for confirmation)
	$(TF_WRAP) $(TF) -chdir=$(TF_DIR) apply -input=false $(ARGS)

.PHONY: tf-destroy
tf-destroy: | ensure-profile-quiet ## Destroy the managed resources (prompts for confirmation)
	$(TF_WRAP) $(TF) -chdir=$(TF_DIR) destroy -input=false $(ARGS)

.PHONY: tf-output
tf-output: | ensure-profile-quiet ## Show the infra outputs
	$(TF_WRAP) $(TF) -chdir=$(TF_DIR) output $(ARGS)

.PHONY: tf-check
tf-check: tf-fmt-check tf-validate ## Run fmt-check + validate (CI-friendly)

.PHONY: tf-clean
tf-clean: ## Remove local Terraform working artifacts (.terraform, lock, plan files)
	@# Never removes backend.tf or .lakebase.env - see tf-state-unwire.
	find $(TF_ROOT) -type d \( -name '.terraform' -o -name '.terraform.validate' \) -prune -exec rm -rf {} +
	find $(TF_ROOT) -type f \( -name '.terraform.lock.hcl' -o -name '*.tfplan' \) -delete

.PHONY: tf-bootstrap-state
tf-bootstrap-state: | ensure-profile-quiet ## Create the Lakebase state project/role/objects and write .lakebase.env (PROFILE=, ARGS=)
	sh terraform/bootstrap/bootstrap-state.sh --profile $(PROFILE) $(ARGS)

.PHONY: tf-snapshot
tf-snapshot: | ensure-profile-quiet ## Create a pre-apply Lakebase branch snapshot of remote state (PROFILE=)
	$(TF_WRAP) sh terraform/bootstrap/snapshot-state.sh --profile $(PROFILE) $(ARGS)

.PHONY: tf-state-info
tf-state-info: | ensure-profile-quiet ## Show remote state host, schema, rows, and current advisory locks
	$(TF_WRAP) sh terraform/bootstrap/state-info.sh $(ARGS)

.PHONY: tf-state-backup
tf-state-backup: | ensure-profile-quiet ## Pull remote state to a timestamped local file (emergency use only)
	$(TF_WRAP) $(TF) -chdir=$(TF_DIR) state pull > "$(TF_DIR)/state-backup-$$(date -u +%Y%m%dT%H%M%SZ).tfstate"

.PHONY: tf-state-unwire
tf-state-unwire: | ensure-profile-quiet ## Step 1 of 2 - stop using the remote backend (see terraform/bootstrap/RUNBOOK.md)
	@test -f $(TF_DIR)/backend.tf || { echo "[tf-state-unwire] $(TF_DIR)/backend.tf already absent."; exit 0; }
	mv $(TF_DIR)/backend.tf $(TF_DIR)/backend.tf.rollback
	@echo "[tf-state-unwire] Moved backend.tf aside. This is NOT yet local state:"
	@echo "                  .terraform still records the pg backend."
	@echo "                  Follow step 2 in terraform/bootstrap/RUNBOOK.md."

.PHONY: test-tfstate
test-tfstate: ## Run the offline Lakebase bootstrap tests (no credentials needed)
	sh terraform/bootstrap/tests/run.sh

