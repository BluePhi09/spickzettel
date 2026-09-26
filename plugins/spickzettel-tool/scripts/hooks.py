#!/usr/bin/env python3
"""Hooks for the memory tool plugin.

  turn   (UserPromptSubmit): touch the turn marker so the MCP server resets its
                             per-turn consolidation-failure budget (max 3, as in hermes-agent).
  guard  (PreToolUse Write|Edit|MultiEdit|NotebookEdit): deny direct edits of
                             MEMORY.md / USER.md; all writes must go through the tool.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import memcore  # noqa: E402


def turn() -> None:
    marker = memcore.memory_dir() / ".turn"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()


def guard() -> None:
    data = json.load(sys.stdin)
    fp = (data.get("tool_input") or {}).get("file_path") or (data.get("tool_input") or {}).get("notebook_path") or ""
    if not fp:
        return
    try:
        path = Path(os.path.expanduser(fp)).resolve()
    except OSError:
        return
    mem_dir = memcore.memory_dir().resolve()
    if path.parent == mem_dir and path.name in ("MEMORY.md", "USER.md"):
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"{path.name} is managed by the `memory` tool (spickzettel-tool). Use the memory tool with "
                f"target='{'user' if path.name == 'USER.md' else 'memory'}' (add/replace/remove, ideally one batch) "
                "instead of editing the file directly."),
        }}))


if __name__ == "__main__":
    try:
        {"turn": turn, "guard": guard}[sys.argv[1]]()
    except Exception as exc:
        print(f"spickzettel-tool hook failed: {exc}", file=sys.stderr)
        sys.exit(0)
