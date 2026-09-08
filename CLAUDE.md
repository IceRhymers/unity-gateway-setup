# CLAUDE.md — unity-gateway-setup

Guidance for Claude Code and other agents that work in this repository.

## Documentation writing standard: ASD-STE100 (Simplified Technical English)

All documentation in this repository follows ASD-STE100 Simplified Technical English (STE). The full standard is installed as a repo-scoped skill at `.claude/skills/asd-ste100/`. Read `.claude/skills/asd-ste100/SKILL.md` before you write or edit any documentation.

**Scope.** This rule covers prose in Markdown docs: `README.md` files, runbooks under `agent_setups/deploy/runbooks/`, changelogs, and any explanatory text. It does not cover code, commands, or generated files. It does not cover vendored files under `.terraform/` or internal state under `.omc/`.

**Modes.** Pick the mode from the document type:

- **Strict** — runbooks, procedures, error messages, and safety text. A wrong reading has a real cost. Apply every rule, including the length caps and one-word-one-meaning discipline.
- **STE-flavored** — `README.md` files, PR descriptions, changelogs, and explanatory prose. Apply the structural rules in full. Treat the lexical one-word-one-meaning rule as advisory.

**Structural rules to apply (both modes).**

1. Use active voice. Name the actor.
2. Use simple tenses (present, past, future). Do not use present perfect, unless a hedge such as "may have failed" needs it.
3. Write one instruction per sentence.
4. Keep sentences short: 20 words or fewer for instructions, 25 words or fewer for descriptions.
5. Do not use phrasal verbs. Write "start", not "spin up". Write "contact", not "reach out". Write "remove", not "take off".
6. Do not use semicolons. Write separate sentences.
7. Keep noun clusters to 3 words or fewer.
8. Do not drop the subject, verb, or article to save space.
9. Do not use marketing adjectives (seamless, robust, powerful, blazing-fast).
10. Use a verb, not a noun, for an action. Write "analyze the log", not "perform an analysis of the log".
11. Use a numbered or bulleted list for 3 or more steps or conditions.
12. Keep one topic per paragraph, 6 sentences or fewer.

**What you must not do.**

- Do not change a fact, command, flag, file path, variable name, or URL. STE applies to prose only.
- Do not invent a cause, a frequency, or a mechanism the source did not state.
- Do not upgrade a hedge to a certainty. "May have failed" is not the same claim as "failed". Keep the hedge.
- Do not drop a safety condition, an exception, or a scope qualifier to shorten a sentence. Keep the longer phrasing instead.

When you write or edit any doc in this repository, apply this standard. To rewrite existing text, invoke the skill (for example: "apply ASD-STE100 to this file").

## Keep the guides in step with the code

This repository ships operator-facing guides: the runbooks under `agent_setups/deploy/runbooks/`, the MDM deployment guides, the VM test runbooks, and the `README.md` files. Each one states commands, file names, key names, and division-of-labour claims that the generators and installers produce. When the code changes, those statements go stale, and a stale runbook is worse than no runbook. An operator follows it and the step fails.

**The rule.** When you change any of the following, re-read every guide that describes it, and update the guide in the same change:

1. A generated file's name, location, or top-level keys.
2. A command an operator runs, or a flag on one.
3. Which component owns a concern (the generator, `ug`, Terraform, or MDM).
4. A prerequisite tool, or how a script authenticates.
5. An exit code, an error message, or a verification step.

**How to find the affected guides.** Search for the thing you changed, not for the topic. Grep the old file name, the old flag, or the old command across `*.md`. Do not trust your memory of which document mentions it.

**Say what changed.** When a guide changes because the code changed, state that in the commit message. A reviewer must be able to see that the doc and the code moved together.

**Do not paper over a contradiction.** If a guide claims something the code no longer does, and you cannot tell which one is correct, stop and ask. Do not edit the guide to match the code, or the code to match the guide, until you know which behaviour is intended.

**Worked example.** The `claude-desktop` generator once moved model selection out of `claude-setup.json` and into a bootstrap script that read `ug`'s state. The change contradicted the division of labour recorded in `README.md`, which assigns the fleet inference baseline to the generator. The generated config lost its Anthropic endpoint, its credential helper, and its models. The guides were rewritten to match the new code, which hid the contradiction instead of surfacing it. Rule 3 exists because of this. Check the ownership table in the root `README.md` before you move a concern between components.
