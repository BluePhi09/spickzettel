# Spickzettel

**Persistent, curated memory for [Claude Code](https://code.claude.com)**: a small, bounded "cheat sheet" (German: *Spickzettel*) that Claude carries into every session, plus full-text search over all your past sessions and a self-maintaining skill library.

> **Beta (0.1.0-beta.2).** Everything works and is tested, but names, config keys and storage layout may still change before 1.0. Please report bugs and ideas in [Issues](https://github.com/BluePhi09/spickzettel/issues). See [CHANGELOG.md](CHANGELOG.md).

Split into plugins you can install individually:

| Plugin | What it does | Requires |
|---|---|---|
| `spickzettel-files` | Curated `MEMORY.md` (max 2,200 chars) and `USER.md` (max 1,375 chars), injected as a **frozen snapshot** at every session start and after every compaction. Direct edits are checked for size, duplicates and injection patterns. | - |
| `spickzettel-tool` | A `memory` tool (local MCP server): `add` / `replace` / `remove` + atomic batch, substring matching, consolidation errors when full (max 3 per turn), dedup, injection/exfiltration scan, file lock + atomic writes. Blocks direct edits of the files. | `spickzettel-files` (installed automatically) |
| `spickzettel-search` | `session_search`: SQLite FTS5 index over **all** Claude Code sessions in all projects. Four modes: search, scroll around a message, read a session, browse recent. Returns real messages, no LLM summaries. | - |
| `spickzettel-skills` | Skills as procedural memory: Claude creates and improves its own skills, `/spickzettel-skills:learn` captures a session, and a weekly curator marks unused agent-created skills stale after 14 days and archives them after 30 (restorable). | - |
| `spickzettel-all` | Bundle that installs all four. | - |

**Capturing a lesson or designing a skill?** Use `/spickzettel-skills:learn` after a session to capture a concrete fix or correction, for example "save the debugging steps we just learned." It prefers updating an existing skill and creates a small one only when needed. For a request such as "design a new skill and test it with examples," use a general skill-creation workflow if one is installed. Spickzettel does not require one.

**Why does the tool depend on the files plugin?** The `memory` tool deliberately has no read action. Claude sees the content through the snapshot. Without `spickzettel-files`, Claude could write memories but never read them back. `spickzettel-files` on its own works fine: Claude then maintains the files with Edit, and a guard hook enforces the rules.

## Install

```
/plugin marketplace add BluePhi09/spickzettel
/plugin install spickzettel-all@spickzettel
```

Or pick individual plugins:

```
/plugin install spickzettel-files@spickzettel     # curated memory files
/plugin install spickzettel-tool@spickzettel      # memory tool (+ files)
/plugin install spickzettel-search@spickzettel    # session search
/plugin install spickzettel-skills@spickzettel    # skill curator
```

Restart Claude Code or run `/reload-plugins` afterwards.

**Requirements:** Claude Code with plugin support (tested with 2.1.283), `python3` ≥ 3.8 on PATH (standard library only), SQLite with FTS5 (bundled with practically every Python; SQLite ≥ 3.34 adds trigram substring search, otherwise it falls back gracefully). Tested on Linux; macOS should work unchanged; Windows has fallbacks but is untested.

**Recommended:** turn off Claude Code's built-in auto memory so two memory systems don't compete. In `~/.claude/settings.json`:

```json
{ "autoMemoryEnabled": false }
```

**Permissions:** Claude Code asks before the first call of each MCP tool, which doubles as a write-approval gate. To save memories without asking:

```json
{ "permissions": { "allow": [
  "mcp__plugin_spickzettel-tool_memory__memory",
  "mcp__plugin_spickzettel-search_sessions__session_search"
] } }
```

## Where data lives

All data lives outside the plugin folders, so uninstalling or updating never deletes it.

| What | Path | Override |
|---|---|---|
| Memory files | `~/.claude/spickzettel/memory/MEMORY.md`, `USER.md` | `SPICKZETTEL_MEMORY_DIR` |
| Memory config | `~/.claude/spickzettel/memory/config.json` | - |
| Session index | `~/.claude/spickzettel/sessions/sessions.db` | `SPICKZETTEL_SESSIONS_DB` |
| Curator state, ledger, archive | `~/.claude/spickzettel/skills/` (`state.json`, `curator_ledger.jsonl`, `archive/`) | `SPICKZETTEL_SKILLS_DIR` |

`~/.claude/spickzettel/memory/config.json` (all optional):

```json
{ "memory_char_limit": 2200, "user_char_limit": 1375, "memory_enabled": true, "user_profile_enabled": true }
```

`~/.claude/spickzettel/skills/config.json` (all optional):

```json
{ "interval_hours": 168, "stale_after_days": 14, "archive_after_days": 30, "archive_enabled": true }
```

Environment variables can be set via the `env` block in `settings.json`.

## How it works

**Memory files.** A `SessionStart` hook fires on `startup`, `resume`, `clear` and `compact`, the moments the context is (re)built. It reads both files and injects them with a usage header such as `MEMORY (your personal notes) [12%, 264/2,200 chars]`, together with guidance on what belongs in memory (declarative, every-session facts; procedures go into skills). Writes during a session hit disk immediately but appear in context only in the next session or after compaction. The snapshot stays stable, which keeps the prompt cache warm. Entries matching a threat pattern are replaced by `[BLOCKED: …]` in the snapshot.

**memory tool.**
- `replace` always overwrites the whole entry; `old_text` only locates it.
- A batch is checked against the limit only after all operations, so one call can free space and add. A batch may not empty a non-empty store.
- If a file was changed externally in a way that wouldn't round-trip, a `.bak.<ts>` is written and the write is refused.
- Success responses deliberately don't echo the entries.
- A `UserPromptSubmit` hook resets the per-turn failure budget; a `PreToolUse` hook blocks direct edits of the two files.

**Session search.**
- Hooks on `SessionStart` (async backfill), `Stop` (async), `PreCompact` and `SessionEnd` index the JSONL transcripts in `~/.claude/projects/` incrementally via byte offsets. The MCP server also catches up before a search (at most every 20 s).
- Subagent transcripts are not indexed.
- The index keeps sessions even after Claude Code deletes transcripts (`cleanupPeriodDays`, default 30 days).
- Extra filter `project` (substring of the working directory).

**Skill curator.**
- Only skills Claude itself creates (a new `…/.claude/skills/<name>/SKILL.md` via Write/Edit, detected by a `PreToolUse` hook) are managed. Your own skills and plugin skills are never touched; `spickzettel-curator track <dir>` opts an existing skill in.
- Use = invocation via the Skill tool or reading its `SKILL.md`; editing also counts as activity.
- The curator runs at session start when the last run is ≥ 7 days ago. Never-used skills younger than 14 days are left alone; pinned skills are never archived; archiving moves the folder to `~/.claude/spickzettel/skills/archive/`; nothing is deleted.

```
spickzettel-curator status | run [--dry-run] | restore <name> | pin <name> | unpin <name> | track <dir> | untrack <name>
```

(or `/spickzettel-skills:curator <command>` inside Claude Code)

## Limitations

- The snapshot lives in session context supplied by the `SessionStart` hook, not in the system prompt proper. It is refreshed only at session start and after compaction.
- No background review / periodic nudges (a forked agent that reviews the conversation every N turns). Until then, Claude saves proactively based on the injected guidance, and `/spickzettel-skills:learn` covers skills.
- The curator only does time-based transitions (active → stale → archived); it doesn't merge overlapping skills with an LLM.
- Claude Code's transcript JSONL format is internal and may change between versions. The parser is tolerant, but an update could affect session search.
- The per-turn failure counter is reset via a shared marker file; concurrent sessions share it (only affects the retry cap).

## Development

`shared/memcore.py` and `shared/mcp_stdio.py` are copied into the plugins (each plugin is installed into its own directory and must be self-contained). After changing them:

```bash
./scripts/sync-shared.sh
claude plugin validate .                 # marketplace
claude plugin validate plugins/<name>
claude --plugin-dir ./plugins            # load all plugins locally for one session
```

## Credits & license

The memory design is inspired by and partly ported from the memory system of [Hermes Agent](https://github.com/NousResearch/hermes-agent) by Nous Research (memory store semantics, threat patterns, tool and guidance texts, curator rules), used under the MIT License, © 2025 Nous Research.

**Spickzettel is an independent project. It is not affiliated with, endorsed by, or sponsored by Nous Research.** "Hermes Agent" is a trademark of Nous Research, Inc.; it is mentioned here only to describe where the design comes from.

MIT License, see [LICENSE](LICENSE).
