#!/usr/bin/env python3
"""PostToolUse hook (Write|Edit|MultiEdit): when MEMORY.md / USER.md were edited
directly, enforce the memory rules (char limit, no duplicates, threat scan) by
feeding problems back to Claude with decision=block."""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import memcore  # noqa: E402


def main() -> None:
    data = json.load(sys.stdin)
    fp = (data.get("tool_input") or {}).get("file_path") or ""
    if not fp:
        return
    try:
        path = Path(os.path.expanduser(fp)).resolve()
    except OSError:
        return
    store = memcore.MemoryStore()
    for target in ("memory", "user"):
        if path == store.path_for(target).resolve():
            problems = memcore.validate_file(target, store)
            if problems:
                print(json.dumps({"decision": "block", "reason": "Memory rules violated: " + " ".join(problems)}))
            return


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"spickzettel-files: guard failed: {exc}", file=sys.stderr)
        sys.exit(0)
