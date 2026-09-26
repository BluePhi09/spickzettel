# AGENTS.md

Guidance for AI coding agents (and humans) working on this repository.

## What this is

Spickzettel is a Claude Code plugin marketplace that gives Claude persistent, curated memory. It consists of four independently installable plugins plus a bundle:

| Plugin | Purpose | Main files |
|---|---|---|
| `spickzettel-files` | Bounded `MEMORY.md` / `USER.md`, injected as a frozen snapshot by a `SessionStart` hook; guard hook for direct edits | `scripts/snapshot_hook.py`, `scripts/guard_hook.py` |
| `spickzettel-tool` | `memory` MCP tool (add / replace / remove / batch); depends on `spickzettel-files` | `server/memory_server.py`, `scripts/hooks.py` |
| `spickzettel-search` | `session_search` MCP tool over a SQLite FTS5 index of Claude Code transcripts | `server/sessiondb.py`, `server/search_server.py`, `scripts/index_hook.py` |
| `spickzettel-skills` | Skill guidance, `/learn`, usage tracking and the time-based skill curator | `scripts/curator.py`, `bin/spickzettel-curator`, `skills/` |
| `spickzettel-all` | Bundle; only a manifest with `dependencies` | `.claude-plugin/plugin.json` |

The marketplace manifest is `.claude-plugin/marketplace.json`. Each plugin has `.claude-plugin/plugin.json` and, where relevant, `hooks/hooks.json` and `.mcp.json`.

## Repository layout

```
.claude-plugin/marketplace.json   marketplace listing all plugins
plugins/<name>/                   one self-contained plugin per folder
shared/memcore.py                 canonical memory store + threat patterns
shared/mcp_stdio.py               canonical minimal MCP stdio server
scripts/sync-shared.sh            copies shared/ modules into the plugins
tests/test_spickzettel.py         stdlib unittest smoke tests for all plugins
.github/workflows/tests.yml       CI: sync check + tests on Linux and macOS
```

## Ground rules

- **Edit shared code only in `shared/`.** `memcore.py` and `mcp_stdio.py` are copied into the plugins because every plugin is installed into its own directory and must be self-contained. After changing them, run `./scripts/sync-shared.sh`. CI fails if the copies are out of sync.
- **Standard library only.** No third-party Python packages. Code must run on Python 3.9 or newer.
- **Hooks must never break a session.** Every hook script catches its own exceptions, writes problems to stderr and exits 0. Keep hooks fast; long work belongs in `async` hooks.
- **Never store state inside a plugin folder.** `${CLAUDE_PLUGIN_ROOT}` changes on every update. User data lives under `~/.claude/spickzettel/` (`memory/`, `sessions/`, `skills/`), overridable via `SPICKZETTEL_MEMORY_DIR`, `SPICKZETTEL_SESSIONS_DB` and `SPICKZETTEL_SKILLS_DIR`. Respect `CLAUDE_CONFIG_DIR`.
- **Keep the memory semantics stable.** Frozen snapshot at session start, char limits counted in characters, entries separated by `\n§\n`, whole-entry `replace`, atomic batches checked against the final budget, threat scan on every write. Changing any of these is a breaking change.
- **The curator only manages skills the agent created.** Never archive or modify user-written or plugin-provided skills, and never delete anything; archiving moves folders to `~/.claude/spickzettel/skills/archive/`.
- **Treat the transcript format as unstable.** Claude Code's JSONL transcripts are internal. The parser in `sessiondb.py` must skip anything it does not understand instead of failing.

## Testing

```bash
./scripts/sync-shared.sh
python3 -m unittest discover -s tests -v
claude plugin validate .
for p in plugins/*; do claude plugin validate "$p"; done
```

To try the plugins in a real session without installing them:

```bash
claude --plugin-dir ./plugins
```

Use temporary directories via the `SPICKZETTEL_*` variables when testing so your own memory and index stay untouched. Add a test in `tests/test_spickzettel.py` for every behavior change.

## Versioning and releases

- All plugins share one version number. Bump `version` in every `.claude-plugin/plugin.json`, in every entry of `.claude-plugin/marketplace.json` and in `metadata.version`, plus the version passed to `Server(...)` in both MCP servers. A test enforces that manifests match.
- Add an entry to `CHANGELOG.md` for every user-visible change.
- The project is in beta (`0.x`); breaking changes are allowed but must be called out in the changelog.
- Plugin names, MCP server keys (`memory`, `sessions`) and tool names (`memory`, `session_search`) are part of the public interface. Users reference them in permission rules such as `mcp__plugin_spickzettel-tool_memory__memory`, so do not rename them casually.

## Style

- Write documentation, tool descriptions and injected guidance in plain English. Do not use em dashes.
- Tool descriptions and guidance texts are read by the model at runtime: keep them precise, declarative and short.
- Commits use the maintainer's GitHub noreply identity. Do not add AI co-author or session trailers to commit messages.

## Credits

The memory design is inspired by and partly ported from Hermes Agent by Nous Research (MIT License). Keep the attribution in `LICENSE`, `README.md` and the headers of ported files. Spickzettel is not affiliated with Nous Research; do not use "Hermes" in plugin, command or package names.
