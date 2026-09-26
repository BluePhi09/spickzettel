#!/usr/bin/env python3
"""MCP server exposing a `memory` tool (add / replace / remove / batch).

No read action on purpose: the content is in context via the frozen snapshot
(spickzettel-files plugin). Semantics follow hermes-agent tools/memory_tool.py."""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import memcore  # noqa: E402
from mcp_stdio import Server  # noqa: E402

TURN_MARKER = memcore.memory_dir() / ".turn"

MEMORY_SCHEMA = {
    "name": "memory",
    "description": (
        "Save durable facts to persistent memory that survive across sessions. Memory is injected into every "
        "future session, so keep entries compact and high-signal.\n\n"
        "HOW: make ALL your changes in ONE call via an 'operations' array (each item: {action, content?, "
        "old_text?}). The batch applies atomically and the char limit is checked only on the FINAL result, so a "
        "single call can remove/replace stale entries to free room AND add new ones, even when an add alone would "
        "overflow. The response reports current/limit chars and confirms completion; one batch call finishes the "
        "update, so don't repeat it. Use the bare action/content/old_text fields only for a single lone change.\n\n"
        "WHEN: only for facts that apply to EVERY session regardless of task: who the user is, stable environment "
        "facts, standing conventions with no task home. Anything learned while doing a task (procedures, pitfalls, "
        "and the user's preferences and corrections for that kind of work) belongs in a skill, where it loads only "
        "when relevant; memory is injected into every session and must stay small.\n\n"
        "IF FULL: an add is rejected with the current entries shown. Reissue as ONE batch that removes or shortens "
        "enough stale entries and adds the new one together.\n\n"
        "TARGETS: 'user' = who the user is (name, role, preferences, style). 'memory' = your notes (environment, "
        "conventions, tool quirks, lessons).\n\n"
        "SKIP: trivial/obvious info, easily re-discovered facts, raw data dumps, task progress, completed-work logs, "
        "temporary TODO state (session_search covers those). Reusable procedures belong in a skill, not memory. "
        "Never store secrets."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["add", "replace", "remove"],
                       "description": "The action to perform (single-op shape). Omit when using 'operations'."},
            "target": {"type": "string", "enum": ["memory", "user"],
                       "description": "Which memory store: 'memory' for personal notes, 'user' for user profile."},
            "content": {"type": "string", "description": (
                "The entry content. Required for 'add' and 'replace'. For 'replace' it is the COMPLETE new entry "
                "text: the whole matched entry is overwritten, so include everything you want to keep. Alias: "
                "'new_text'.")},
            "old_text": {"type": "string", "description": (
                "REQUIRED for 'replace' and 'remove' (single-op shape): a short unique substring IDENTIFYING the "
                "existing entry -- it locates the entry, it is not spliced out. Omit only for 'add'.")},
            "new_text": {"type": "string", "description": "Alias for 'content'. If both are set, 'content' wins."},
            "operations": {
                "type": "array",
                "description": ("Batch shape: operations applied atomically in one call against the final char "
                                "budget. Preferred when making multiple changes or consolidating to make room."),
                "items": {"type": "object", "properties": {
                    "action": {"type": "string", "enum": ["add", "replace", "remove"]},
                    "content": {"type": "string", "description": "Entry content for add/replace (COMPLETE entry)."},
                    "new_text": {"type": "string", "description": "Alias for 'content'."},
                    "old_text": {"type": "string", "description": "Substring identifying the entry for replace/remove."},
                }, "required": ["action"]},
            },
        },
        "required": ["target"],
    },
}


class State:
    store = memcore.MemoryStore()
    turn_mtime = 0.0


def _maybe_reset_turn() -> None:
    """The UserPromptSubmit hook touches .turn each user turn: reset the per-turn failure budget."""
    try:
        mtime = TURN_MARKER.stat().st_mtime
    except OSError:
        return
    if mtime != State.turn_mtime:
        State.turn_mtime = mtime
        State.store.reset_consolidation_failures()


def memory_tool(args):
    store = State.store
    _maybe_reset_turn()
    target = args.get("target") or "memory"
    action = args.get("action")
    content = args.get("content")
    if content is None:
        content = args.get("new_text")
    old_text = args.get("old_text")
    operations = args.get("operations")
    if target not in ("memory", "user"):
        return json.dumps({"success": False, "error": f"Invalid memory target '{target}'. Use 'memory' or 'user'."})
    if not store.target_enabled(target):
        name = "USER.md" if target == "user" else "MEMORY.md"
        return json.dumps({"success": False, "error": f"{name} writes are disabled in config.json."})
    if operations:
        if not isinstance(operations, list):
            return json.dumps({"success": False, "error": "operations must be a list of {action, content?, old_text?} objects."})
        return json.dumps(store.apply_batch(target, operations), ensure_ascii=False)
    if action not in ("add", "replace", "remove"):
        return json.dumps({"success": False, "error": f"Unknown action '{action}'. Use: add, replace, remove"})
    if action == "add" and not content:
        return json.dumps({"success": False, "error": "Content is required for 'add' action."})
    if action in ("replace", "remove") and not old_text:
        store.load_from_disk()
        hint = (" For 'replace', content is the COMPLETE new entry -- the whole matched entry is overwritten."
                if action == "replace" else "")
        return json.dumps({"success": False, "error": (
            f"'{action}' needs old_text -- a short unique substring of the entry to {action}. None was provided. "
            f"Reissue the {action} with old_text set to part of one of the current_entries below.{hint}"),
            "current_entries": store.entries_for(target), "usage": store.usage(target)}, ensure_ascii=False)
    if action == "replace" and not content:
        return json.dumps({"success": False, "error": "content is required for 'replace' action."})
    result = {"add": lambda: store.add(target, content),
              "replace": lambda: store.replace(target, old_text, content),
              "remove": lambda: store.remove(target, old_text)}[action]()
    return json.dumps(result, ensure_ascii=False)


def main() -> None:
    server = Server("spickzettel-memory", "1.0.0", instructions=(
        "Curated persistent memory. Use the `memory` tool to save durable, every-session facts to MEMORY.md "
        "(your notes) or USER.md (user profile). There is no read action: the current content is in your context "
        "as a snapshot from session start."))
    server.tool(MEMORY_SCHEMA, memory_tool)
    server.run()


if __name__ == "__main__":
    main()
