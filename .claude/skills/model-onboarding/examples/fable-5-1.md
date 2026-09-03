# Worked example — bump claude-fable to 5-1

This example shows a full Case A run. The request: add `system.ai.claude-fable-5-1`.

## Classify

The `claude-fable` alias already exists and points to `system.ai.databricks-claude-fable-5`. The new model is a point-version bump of that family. This is Case A.

Note the naming. The request said `system.ai.claude-fable-5-1`. The repo convention is `system.ai.databricks-<name>`, so the entity is `system.ai.databricks-claude-fable-5-1` and the derived endpoint is `claude-fable-5-1`.

## Questions and answers

1. **Alias bump?** Answer: bump. Repoint `claude-fable` to `system.ai.databricks-claude-fable-5-1`. The `fable` tier and the `claude-fable` endpoint then serve 5-1 at once.
2. **Prior version?** Answer: keep. `claude-fable-5` stays a versioned pin. The family now holds the latest (5-1) plus one prior (5), which matches the curated-recent policy.
3. **Tier fallback?** Answer: yes. Prefer the new pin over the old one in the fallback order.

## Edits

### `terraform/infra/variables.tf`

The alias, inside `anthropic.aliases`:

```hcl
# before
"claude-fable"  = "system.ai.databricks-claude-fable-5"
# after
"claude-fable"  = "system.ai.databricks-claude-fable-5-1"
```

The pin, inside `anthropic.versioned_models`:

```hcl
# before
"system.ai.databricks-claude-haiku-4-5",
"system.ai.databricks-claude-fable-5",

# after
"system.ai.databricks-claude-haiku-4-5",
"system.ai.databricks-claude-fable-5-1",
"system.ai.databricks-claude-fable-5",
```

### `agent_setups/scripts/agents/claude_code.py`

`TIER_PREFERENCES`, the `fable` tier:

```python
# before
"fable": ["claude-fable", "claude-fable-5"],
# after
"fable": ["claude-fable", "claude-fable-5-1", "claude-fable-5"],
```

The alias `claude-fable` still resolves first, so the tier tracks the bump on its own. The fallback change matters only when the alias endpoint is absent.

`TIER_CAPABILITIES`, `TIER_DISPLAY`, and `LARGE_CONTEXT_FAMILIES` need no edit. The `fable` tier already exists, and fable keeps its native context window.

### Other generators

`codex.py`, `opencode.py`, and `dsh.py` need no edit. None of their defaults route to fable.

### Docs

`agent_setups/scripts/README.md` names the alias `claude-fable`, not the version, so it needs no edit.

## Verify

1. From `agent_setups/scripts`: `python -m pytest tests/ -q`. Result: 194 passed.
2. `terraform -chdir=terraform/infra fmt -check`. Result: clean.
