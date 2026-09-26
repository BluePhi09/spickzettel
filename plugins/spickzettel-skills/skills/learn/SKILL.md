---
name: learn
description: Capture what was learned in this session as a reusable skill: create a new SKILL.md or patch an existing one (procedural memory).
---

Review the conversation so far and update the skill library. Be active: most sessions that involved real work produce at least one skill update, even a small one.

Focus requested by the user (may be empty): $ARGUMENTS

## Signals worth capturing
- The user corrected your style, format, verbosity or approach for a kind of task ("stop doing X", "too verbose", "remember this").
- A non-trivial technique, fix, workaround, debugging path or tool-usage pattern emerged that a future session would benefit from.
- A skill used in this session turned out to be wrong, missing a step, or outdated. Patch it now.

## Preference order (pick the earliest that fits)
1. **Patch a skill that was used in this session** and covers the territory (read its SKILL.md first).
2. **Patch an existing class-level skill** in `~/.claude/skills/` or `.claude/skills/` that covers it: add a step, a pitfall, or broaden its trigger.
3. **Add a support file** under an existing skill: `references/<topic>.md` for depth needed only sometimes, `templates/` for starter files, `scripts/` for re-runnable actions.
4. **Create a new class-level skill** at `~/.claude/skills/<kebab-name>/SKILL.md` (or `.claude/skills/` if project-specific).

## Rules
- Read before write: open the current SKILL.md before editing it.
- Frontmatter: `name` + `description`; the first ~60 characters of the description must say WHEN to use the skill.
- Body: When to Use / Procedure / Pitfalls / Verification. Keep SKILL.md focused; move depth into `references/`.
- Do not modify skills that came from plugins or that the user wrote themselves unless the user asks.
- Do not capture: environment failures, negative claims about tools, one-off narratives, unresolved problems, secrets.
- Facts that apply to every session regardless of task (who the user is, stable environment facts) belong in memory, not in a skill.

Finish with one short line per skill created or changed (name + what changed), or "Nothing worth capturing." if there truly was nothing.
