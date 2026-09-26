---
name: curator
description: Show or manage the Spickzettel skill curator: status of agent-created skills, run a curation pass, restore/pin/unpin/track skills.
disable-model-invocation: true
---

Run the curator CLI with the user's arguments (default `status`) and report the result briefly:

```bash
spickzettel-curator ${ARGUMENTS:-status}
```

Available commands: `status`, `run [--dry-run]`, `restore <name>`, `pin <name>`, `unpin <name>`, `track <skill-dir>`, `untrack <name>`.

User arguments: $ARGUMENTS

If `spickzettel-curator` is not on PATH, run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/curator.py"` with the same arguments.
