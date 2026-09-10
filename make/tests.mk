# tests.mk — Test suites and the aggregate static check.
#
# Included from the root Makefile, which defines every variable used here
# (PROFILE, TF_WRAP, AGENT_GEN, OUT_DIR, VERSION, ...). Do not run this file
# directly. `make help` lists these targets: it greps $$(MAKEFILE_LIST), so an
# included target's `## comment` shows up with no extra wiring.

# ---- tests ----

.PHONY: test
test: ## Run the deploy install.sh test suite (self-contained: no infra, no network, no pre-generated bundles)
	sh agent_setups/deploy/tests/run.sh

.PHONY: test-generators
test-generators: ## Run the Python unit tests for the config generators (needs Python 3.11+ and: pip install -r agent_setups/scripts/requirements.txt)
	$(PYTHON) -m unittest discover -s agent_setups/scripts/tests -v

.PHONY: check
check: tf-fmt-check tf-validate test test-generators test-tfstate ## Run all static checks (tf-fmt-check + tf-validate + deploy tests + generator unit tests; no creds needed)

