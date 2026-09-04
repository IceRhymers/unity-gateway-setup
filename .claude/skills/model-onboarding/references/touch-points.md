# Touch-point map

Every place a model reaches the surface. Line numbers drift, so search by the symbol name, not the line.

## Layer 1 — Terraform (source of truth)

### `terraform/infra/variables.tf` → `model_providers` default
The one required edit for every model change. Each provider object holds:

- **`aliases`** — a map of versionless name to `system.ai.databricks-<name>`. An alias tracks the current best model in a family. The map key becomes the endpoint name.
- **`versioned_models`** — a list of `system.ai.databricks-<name>`. Each entry gets a version-pinned endpoint. The endpoint name drops the `databricks-` prefix.

Policy for `versioned_models`: "curated recent". Keep the latest of each aliased family plus one recent prior. Do not pin every historical version.

### `terraform/infra/main.tf` — no manual edit
`locals.strip` derives the endpoint name from the model name. `locals.services` merges aliases and versioned models into one endpoint map keyed by `<schema>/<endpoint>`. Read this to predict the endpoint name. Do not edit it for a model add.

### `terraform/modules/model-service/` — no manual edit
The reusable module that creates one serving endpoint. It accepts `foundation_model`. A model add does not change the module.

## Layer 2 — Agent generators

### `agent_setups/scripts/agents/claude_code.py`
- **`TIER_PREFERENCES`** — for each tier (`opus`, `sonnet`, `haiku`, `fable`), an ordered list of endpoint leaf names. The generator resolves the tier to the first match that exists. Put the alias first, then version pins as fallback.
- **`TIER_CAPABILITIES`** — the capability string per tier. Edit only for a new tier.
- **`TIER_DISPLAY`** — the UI label per tier. Edit only for a new tier.
- **`LARGE_CONTEXT_FAMILIES`** — the families that default to the `[1m]` window. Add a new tier here only when it defaults to 1M context.
- `availableModels` needs no edit. The generator discovers every Anthropic-capable endpoint live per workspace.

### `agent_setups/scripts/agents/codex.py`
- **`DEFAULT_MODEL_PREFERENCES`** — the ordered fallback for the codex default model. Edit only when the codex default must change.

### `agent_setups/scripts/agents/dsh.py`
- **`DEFAULT_MODEL_PREFERENCES`** — the DeepSeek Harness default order. DeepSeek routes through the OSS mlflow surface (`GATEWAY_OSS_ROUTE`).

## Layer 3 — Tests and docs

### `agent_setups/scripts/tests/`
- `test_claude_code_generator.py`, `test_claude_desktop_generator.py`, `test_codex_generator.py`, `test_dsh_generator.py`. Add or update a test when a tier mapping, a default, or a route changes.

### Docs
- `agent_setups/scripts/README.md` — the "What the Claude Code config encodes" section names the tier mapping and the pin strategy. Update it when either changes. It names aliases (`fable`→`claude-fable`), so a version bump alone needs no change here.
- `terraform/infra/README.md` and `terraform/README.md` — the alias-versus-versioned explanation.

## Edit order

1. `terraform/infra/variables.tf`.
2. `claude_code.py` tier maps, if a tier is involved.
3. The other agent generators, only if a default or route changes.
4. Tests.
5. Docs.
