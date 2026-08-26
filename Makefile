# unity-gateway-setup — main task executor
#
# Run `make` or `make help` to list targets.

# ---- configuration (override on the command line, e.g. `make tf-plan TF_DIR=...`) ----
TF        ?= terraform
TF_ROOT   ?= terraform
TF_DIR    ?= terraform/infra
ARGS      ?=

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
