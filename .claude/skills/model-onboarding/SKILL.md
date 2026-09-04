---
name: model-onboarding
description: "Use when you add, bump, or remove a Databricks foundation model on this gateway surface (a system.ai.databricks-* model exposed through the Unity AI Gateway). Triggers: add a model, onboard a model, bump the alias, add claude/gpt/gemini/deepseek, new model version, add a provider schema, deprecate a model, wire a model into agent configs. Drives the edits across terraform/infra and agent_setups so a new model reaches Terraform, Claude Code, Claude Desktop, codex, and dsh consistently."
version: 0.1.0
---

# Model onboarding

This skill onboards a new Databricks foundation model into this gateway surface. The surface has two layers:

1. **Terraform** (`terraform/infra`) provisions one Unity Catalog serving endpoint per model. This layer is the source of truth.
2. **Agent generators** (`agent_setups/scripts`) read the Terraform endpoints and write config for Claude Code, Claude Desktop, codex, and dsh.

A model reaches an agent only after both layers know about it. This skill asks the right questions for the model type, then applies the edits in the right places.

## Before you start

Confirm the model exists as a `system.ai.databricks-<name>` foundation model in the target workspace. Terraform creates the serving endpoint. Terraform does not create the underlying foundation model. An apply against a model the workspace does not publish fails at the endpoint create step.

The repo uses one naming convention. Every entity is `system.ai.databricks-<name>`. The endpoint name drops the `databricks-` prefix. For example, `system.ai.databricks-claude-fable-5-1` becomes the endpoint `claude-fable-5-1`. `terraform/infra/main.tf` derives the endpoint name automatically. Do not add an endpoint name by hand.

## Step 1 — Classify the model

Pick the case that matches the request. The case decides which questions to ask.

- **Case A — point-version bump of an existing aliased family.** A newer version of a family that already has an alias. Example: `claude-fable-5` becomes `claude-fable-5-1`. The alias `claude-fable` already exists.
- **Case B — new sibling under an existing provider.** A new model that needs its own alias in a provider schema that already exists. Example: a new GPT line beside `gpt-sol`.
- **Case C — new Claude Code tier.** A model that needs a new tier slot (`opus`, `sonnet`, `haiku`, `fable`). This is Claude only. Case C almost always also runs Case A or Case B for the Terraform layer.
- **Case D — new provider schema.** A provider that has no schema yet. Example: a first model from a new vendor.
- **Case E — removal or deprecation.** Remove a model from the surface.

## Step 2 — Ask the questions for the case

Ask these with `AskUserQuestion` before you edit. Ask early. A wrong guess forces a rebuild.

### Case A — point-version bump

1. **Alias bump (required).** Repoint the family alias to the new version, or add a pinned endpoint only? A bump makes the alias, the default tier, and every alias consumer serve the new version at once. A pin-only add leaves the alias on the old version and exposes the new version through its version-pinned endpoint only.
2. **Prior version (default: keep).** Keep the previous version as a versioned pin, or drop it? The repo policy is "curated recent": the latest of each family plus one recent prior. Keep the prior version unless the user asks to drop it.
3. **Tier fallback (default: yes).** Update the `TIER_PREFERENCES` fallback order for the tier so it prefers the new pin over the old one? The alias still resolves first, so this changes behavior only when the alias endpoint is absent.

### Case B — new sibling alias

1. **Alias name (required).** What alias name does the model get? Keep the noun cluster to 3 words or fewer. Drop the `databricks-` prefix. Follow the sibling naming (for example `gpt-luna` beside `gpt-sol`).
2. **Versioned pin (default: yes).** Add the model to `versioned_models` as well, so it also gets a version-pinned endpoint?
3. **Default preference (default: no).** Does any Claude Code tier or any agent default preference route to this model? Most siblings are opt-in and change no default.

### Case C — new tier

1. **Tier key and label (required).** What tier key (lowercase) and display label does the model get?
2. **Capabilities (required).** Which capabilities does the tier declare (`effort`, `thinking`, `adaptive_thinking`, `interleaved_thinking`, `xhigh_effort`, `max_effort`)? Custom gateway model IDs skip auto-detection, so you must declare these.
3. **Large context (default: no).** Does the family default to the `[1m]` (1M) context window?

### Case D — new provider schema

1. **Schema key and comment (required).** What schema key (the provider name) and schema comment does the provider get?
2. **Alias and version sets (required).** Which aliases and which versioned models does the schema start with?
3. **Gateway surface (required).** Which API surface does the provider expose: the Anthropic Messages surface, the Gemini surface, or the OpenAI-compatible mlflow surface? The answer decides whether the model appears in Claude Code `availableModels` discovery and which gateway route the agent generators use.

### Case E — removal

1. **Alias target check (required).** Is the model an alias target? If yes, repoint or remove the alias first, so no alias points to a missing model.
2. **Pin removal (required).** Remove the model from `versioned_models`.
3. **Consumer check (required).** Search for the model name across the agent generators. Remove any tier preference or default preference that names it.

## Step 3 — Apply the edits

Edit only the locations the case needs. `references/touch-points.md` lists every location with its file and purpose. The short list:

1. `terraform/infra/variables.tf` — the `model_providers` default. Edit `aliases` and `versioned_models`. This is always the first edit.
2. `agent_setups/scripts/agents/claude_code.py` — `TIER_PREFERENCES`, and for a new tier also `TIER_CAPABILITIES`, `TIER_DISPLAY`, and `LARGE_CONTEXT_FAMILIES`.
3. `agent_setups/scripts/agents/codex.py`, `dsh.py` — the `DEFAULT_MODEL_PREFERENCES` in each. Edit these only when a default must change.
4. `agent_setups/scripts/tests/` — add or update the generator tests.
5. `agent_setups/scripts/README.md` and the Terraform READMEs — update the model text only when a tier mapping or the pin strategy changes.

## Step 4 — Verify

Run both checks before you report the work as done.

1. Run the generator tests. From `agent_setups/scripts`, run `python -m pytest tests/ -q`.
2. Check the Terraform. Run `terraform -chdir=terraform/infra fmt -check` and `terraform -chdir=terraform/infra validate`. A plan against the live workspace confirms the endpoint resolves.

## Step 5 — Document

State the change in the PR description and any changelog. Name the model, the alias decision, and the tier decision. Follow the repo ASD-STE100 standard for the prose.

## Worked example

`examples/fable-5-1.md` shows a full Case A run: the questions asked, the answers, and every edit for the `claude-fable-5-1` bump.
