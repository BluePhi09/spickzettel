#!/usr/bin/env python3
"""SessionStart hook: inject the FROZEN snapshot of MEMORY.md + USER.md.

Runs on startup, resume, clear and compact, i.e. exactly the points where hermes-agent
(re)builds its system prompt. Mid-session writes land on disk but only show up here
at the next session start or after a compaction (prefix-cache friendly)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import memcore  # noqa: E402

GUIDANCE = (
    "You have persistent memory, carried across sessions and loaded into each new session's context. "
    "Skills come first: when you learn something while doing a task (a procedure, a pitfall, and the "
    "user's preferences and corrections for that kind of work), record it in a skill, where it loads only "
    "when relevant. Memory is the narrow exception for facts that apply to EVERY session regardless of task "
    "(who the user is, environment facts, standing conventions with no task home); it has a hard character "
    "budget, so when it fills, replace or consolidate stale entries rather than skipping the save. Write "
    "entries as declarative facts, not instructions to yourself: 'User prefers concise responses' ✓, "
    "'Always respond concisely' ✗ (imperative phrasing gets re-read as a directive in later sessions and can "
    "override the user's current request). A fact stale within a week belongs in session history; "
    "procedures and workflows belong in skills. Save proactively when you learn such a fact; do not wait "
    "to be asked. Never store secrets, credentials or tokens."
)


def main() -> None:
    try:
        json.load(sys.stdin)
    except ValueError:
        pass
    store = memcore.MemoryStore()
    store.load_from_disk()
    mem_path, user_path = store.path_for("memory"), store.path_for("user")
    how = (
        "HOW TO SAVE: if a `memory` tool is available (spickzettel-tool plugin), use ONLY that tool "
        "(add/replace/remove, preferably one batch call). Otherwise edit the files directly with Edit/Write: "
        f"`{mem_path}` (your notes, max {store.memory_char_limit:,} chars) and `{user_path}` (user profile, max "
        f"{store.user_char_limit:,} chars). Entries are separated by a line containing only `§`. "
        "The memory block below is a frozen snapshot from session start: your writes are saved immediately "
        "but appear here only in the next session or after compaction."
    )
    parts = ["# Persistent memory (Spickzettel)", GUIDANCE, how]
    blocks = []
    if store.memory_enabled:
        blocks.append(store.render_block("memory"))
    if store.user_profile_enabled:
        blocks.append(store.render_block("user"))
    blocks = [b for b in blocks if b]
    parts.append("\n\n".join(blocks) if blocks else "(Memory is currently empty.)")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                             "additionalContext": "\n\n".join(parts)}}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # a broken hook must never block the session
        print(f"spickzettel-files: snapshot failed: {exc}", file=sys.stderr)
        sys.exit(0)
