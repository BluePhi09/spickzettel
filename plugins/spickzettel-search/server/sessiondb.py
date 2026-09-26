"""SQLite FTS5 index over Claude Code session transcripts (design follows hermes-agent's session_search).

Reads ~/.claude/projects/<project>/<session-id>.jsonl incrementally (byte offsets),
so it is cheap to call from hooks after every turn. The index keeps sessions even after
Claude Code's own transcript cleanup (cleanupPeriodDays) deletes the .jsonl files.

Note: the transcript JSONL format is internal to Claude Code and may change between
versions. The parser is deliberately tolerant: unknown line types are skipped.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

TOOL_CONTENT_PREFIX_CHARS = 8192
TOOL_CALLS_CHARS = 2000
_REMINDER_RX = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS sessions(
    id TEXT PRIMARY KEY, project TEXT, cwd TEXT, git_branch TEXT, title TEXT,
    started_at TEXT, last_at TEXT, message_count INTEGER DEFAULT 0, source_path TEXT);
CREATE TABLE IF NOT EXISTS messages(
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, ukey TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL, content TEXT, tool_name TEXT, tool_calls TEXT, timestamp TEXT);
CREATE INDEX IF NOT EXISTS messages_session ON messages(session_id, id);
CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, offset INTEGER, session_id TEXT);
CREATE TABLE IF NOT EXISTS tool_use_names(tool_use_id TEXT PRIMARY KEY, name TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content, tool_name, tool_calls, content='messages', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content, tool_name, tool_calls)
    VALUES (new.id, new.content, new.tool_name, new.tool_calls);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content, tool_name, tool_calls)
    VALUES ('delete', old.id, old.content, old.tool_name, old.tool_calls);
END;
"""

TRIGRAM_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts_trigram USING fts5(
    content, content='messages', content_rowid='id', tokenize='trigram');
CREATE TRIGGER IF NOT EXISTS messages_ai_tri AFTER INSERT ON messages
WHEN new.role IN ('user', 'assistant') BEGIN
    INSERT INTO messages_fts_trigram(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad_tri AFTER DELETE ON messages
WHEN old.role IN ('user', 'assistant') BEGIN
    INSERT INTO messages_fts_trigram(messages_fts_trigram, rowid, content) VALUES ('delete', old.id, old.content);
END;
"""


def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()


def db_path() -> Path:
    env = os.environ.get("SPICKZETTEL_SESSIONS_DB")
    return Path(env).expanduser() if env else claude_home() / "spickzettel" / "sessions" / "sessions.db"


def projects_dir() -> Path:
    return claude_home() / "projects"


def connect(path: Optional[Path] = None) -> Tuple[sqlite3.Connection, bool]:
    """Open (and create) the DB. Returns (conn, has_trigram)."""
    path = path or db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    conn.executescript(SCHEMA)
    has_trigram = True
    try:
        conn.executescript(TRIGRAM_SCHEMA)
    except sqlite3.OperationalError:  # SQLite < 3.34: no trigram tokenizer, search degrades gracefully
        has_trigram = False
    return conn, has_trigram


# ─────────────────────────── transcript parsing ───────────────────────────

def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text") or "")
            elif isinstance(b, str):
                parts.append(b)
        return "\n".join(parts)
    return ""


def _clean_user_text(text: str) -> str:
    return _REMINDER_RX.sub("", text).strip()


def parse_line(d: Dict[str, Any], tool_names: Dict[str, str]) -> List[Dict[str, Any]]:
    """Rows for one transcript line (may be several: text + tool results)."""
    t = d.get("type")
    if t not in ("user", "assistant") or d.get("isSidechain") or d.get("isMeta") or d.get("isCompactSummary"):
        return []
    msg = d.get("message") or {}
    content = msg.get("content")
    uuid = d.get("uuid") or ""
    ts = d.get("timestamp") or ""
    rows: List[Dict[str, Any]] = []
    if t == "user":
        if isinstance(content, str):
            text = _clean_user_text(content)
            if text:
                rows.append({"role": "user", "content": text})
        elif isinstance(content, list):
            texts = []
            for i, b in enumerate(content):
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":
                    texts.append(b.get("text") or "")
                elif b.get("type") == "tool_result":
                    body = _text_of(b.get("content"))[:TOOL_CONTENT_PREFIX_CHARS]
                    if body:
                        rows.append({"role": "tool", "content": body, "part": f"r{i}",
                                     "tool_name": tool_names.get(b.get("tool_use_id") or "", "")})
            text = _clean_user_text("\n".join(texts))
            if text:
                rows.insert(0, {"role": "user", "content": text})
    else:
        texts, calls = [], []
        if isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":
                    texts.append(b.get("text") or "")
                elif b.get("type") == "tool_use":
                    name = b.get("name") or ""
                    if b.get("id"):
                        tool_names[b["id"]] = name
                    calls.append({"name": name, "input": b.get("input")})
        else:
            texts.append(_text_of(content))
        text = "\n".join(x for x in texts if x).strip()
        if text or calls:
            rows.append({"role": "assistant", "content": text,
                         "tool_name": ",".join(c["name"] for c in calls) or None,
                         "tool_calls": json.dumps(calls, ensure_ascii=False)[:TOOL_CALLS_CHARS] if calls else None})
    for n, r in enumerate(rows):
        r["ukey"] = f"{uuid}:{r.pop('part', n)}"
        r["timestamp"] = ts
    return rows


# ─────────────────────────── indexing ───────────────────────────

class _ToolNames(dict):
    """tool_use_id -> tool name; falls back to the DB for ids from earlier chunks."""

    def __init__(self, conn: sqlite3.Connection):
        super().__init__()
        self.conn, self.new = conn, {}

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self.new[key] = value

    def get(self, key, default=None):
        if key in self:
            return self[key]
        row = self.conn.execute("SELECT name FROM tool_use_names WHERE tool_use_id=?", (key,)).fetchone()
        return row["name"] if row else default


def _session_id_for(conn: sqlite3.Connection, path: Path) -> str:
    """Transcript stem (= Claude Code session id, usable with --resume); disambiguated with the
    project dir if another transcript file already claimed the same id."""
    other = conn.execute("SELECT source_path FROM sessions WHERE id=?", (path.stem,)).fetchone()
    if other is None or other["source_path"] == str(path):
        return path.stem
    return f"{path.stem}@{path.parent.name}"


def index_file(conn: sqlite3.Connection, path: Path) -> int:
    """Incrementally index one transcript. Returns number of new rows."""
    path = path.resolve()
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    row = conn.execute("SELECT offset, session_id FROM files WHERE path=?", (str(path),)).fetchone()
    offset = row["offset"] if row else 0
    session_id = row["session_id"] if row else _session_id_for(conn, path)
    if size == offset:
        return 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT offset FROM files WHERE path=?", (str(path),)).fetchone()  # re-read under lock
        offset = row["offset"] if row else 0
        if size < offset:  # file was rewritten: re-index from scratch
            conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
            offset = 0
        with open(path, "rb") as f:
            f.seek(offset)
            chunk = f.read(size - offset)
        end = chunk.rfind(b"\n")
        if end < 0:
            conn.execute("COMMIT")
            return 0
        chunk = chunk[:end + 1]
        tool_names = _ToolNames(conn)
        meta = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        meta = dict(meta) if meta else {"id": session_id, "project": path.parent.name, "cwd": None, "git_branch": None,
                                          "title": None, "started_at": None, "last_at": None, "message_count": 0}
        added = 0
        for raw in chunk.splitlines():
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            ts = d.get("timestamp")
            if d.get("type") in ("user", "assistant") and not d.get("isSidechain"):
                meta["cwd"] = meta["cwd"] or d.get("cwd")
                meta["git_branch"] = d.get("gitBranch") or meta["git_branch"]
                if ts:
                    meta["started_at"] = min(filter(None, [meta["started_at"], ts]))
                    meta["last_at"] = max(filter(None, [meta["last_at"], ts]))
            for r in parse_line(d, tool_names):
                cur = conn.execute(
                    "INSERT OR IGNORE INTO messages(session_id, ukey, role, content, tool_name, tool_calls, timestamp) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (session_id, f"{session_id}:{r['ukey']}", r["role"], r["content"], r.get("tool_name"), r.get("tool_calls"),
                     r["timestamp"]))
                if cur.rowcount:
                    added += 1
                    if r["role"] == "user" and not meta["title"]:
                        meta["title"] = " ".join(r["content"].split())[:100]
        for k, v in tool_names.new.items():
            conn.execute("INSERT OR REPLACE INTO tool_use_names VALUES (?,?)", (k, v))
        meta["message_count"] = conn.execute("SELECT COUNT(*) FROM messages WHERE session_id=?",
                                             (session_id,)).fetchone()[0]
        conn.execute(
            "INSERT OR REPLACE INTO sessions(id, project, cwd, git_branch, title, started_at, last_at, message_count, "
            "source_path) VALUES (?,?,?,?,?,?,?,?,?)",
            (session_id, meta["project"], meta["cwd"], meta["git_branch"], meta["title"], meta["started_at"],
             meta["last_at"], meta["message_count"], str(path)))
        conn.execute("INSERT OR REPLACE INTO files(path, offset, session_id) VALUES (?,?,?)",
                     (str(path), offset + end + 1, session_id))
        conn.execute("COMMIT")
        return added
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def iter_transcripts() -> Iterable[Path]:
    root = projects_dir()
    if root.is_dir():
        yield from root.glob("*/*.jsonl")  # top level only: subagent sidechains are excluded (as in hermes-agent)


def index_all(conn: sqlite3.Connection) -> int:
    total = 0
    for p in iter_transcripts():
        try:
            total += index_file(conn, p)
        except (OSError, sqlite3.Error):
            continue
    return total
