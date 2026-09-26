# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/). All plugins in this marketplace share one version number.

## [0.1.0-beta.1] - 2026-09-26

First public beta.

### Added

- `spickzettel-files`: bounded `MEMORY.md` (2,200 chars) and `USER.md` (1,375 chars), injected as a frozen snapshot on session start, resume, clear and compaction. Guard hook checks direct edits for size, duplicates and injection patterns.
- `spickzettel-tool`: `memory` MCP tool with add, replace, remove and atomic batches, substring matching, consolidation errors when full (max 3 failures per turn), dedup, injection and exfiltration scan, file locking and atomic writes. Blocks direct edits of the memory files.
- `spickzettel-search`: `session_search` MCP tool backed by an incremental SQLite FTS5 index of all Claude Code transcripts, with search, scroll, read and browse modes, time and project filters, and trigram substring search where available.
- `spickzettel-skills`: guidance for self-created skills, `/spickzettel-skills:learn`, usage tracking, and a weekly curator that marks unused agent-created skills stale after 14 days and archives them after 30. CLI `spickzettel-curator` with status, run, restore, pin, unpin, track and untrack.
- `spickzettel-all`: bundle that installs all four plugins.
- Smoke tests and CI on Linux and macOS.

### Known limitations

- No background review or periodic nudges yet.
- The curator only performs time-based transitions; it does not merge overlapping skills.
- Claude Code's transcript format is internal and may change between versions.

[0.1.0-beta.1]: https://github.com/BluePhi09/spickzettel/commits/main
