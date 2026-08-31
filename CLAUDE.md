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
