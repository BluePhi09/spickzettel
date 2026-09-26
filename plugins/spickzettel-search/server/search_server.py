#!/usr/bin/env python3
"""MCP server exposing a `session_search` tool over Claude Code transcripts.

Four shapes, picked by args (as in hermes-agent tools/session_search_tool.py):
  query                         -> DISCOVERY  (FTS5/BM25, grouped by session)
  session_id + around_message_id-> SCROLL     (window around an anchor message)
  session_id                    -> READ       (head/tail of a whole session)
  (no args)                     -> BROWSE     (recent sessions)
Results are actual stored messages, no LLM summarization."""

import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import sessiondb  # noqa: E402
from mcp_stdio import Server  # noqa: E402

READ_MAX_CONTENT = 2000
SNIPPET_CHARS = 600
REFRESH_INTERVAL_S = 20

SCHEMA = {
    "name": "session_search",
    "description": (
        "Recall past Claude Code conversations: search or read old sessions (SQLite FTS5 over all transcripts, "
        "across all projects), or scroll inside one. Four shapes, picked by args: `query` = discovery (top-N "
        "matching sessions, top result fully hydrated); `session_id` + `around_message_id` = scroll (window of "
        "messages around an anchor); `session_id` alone = read a whole session; no args = browse recent sessions. "
        "Results are actual stored messages, no LLM. Searches conversation history ONLY. When the user gave a "
        "direct source (URL, file, live system), inspect that first; never conclude 'not found' from history "
        "alone. Use it when the user references something from a past conversation ('what did we do about X', "
        "'where did we leave Y') or you suspect relevant cross-session context exists, before asking the user to "
        "repeat themselves. The current session may appear in results too."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": (
                "Search query (discovery shape). Keywords, phrases (\"exact phrase\"), or FTS5 boolean expressions "
                "(AND/OR/NOT, prefix*). Omit to browse recent sessions.")},
            "limit": {"type": "integer", "default": 3, "description": (
                "Discovery/browse. Max sessions to return (discovery default 3, max 10; browse default 10, max 50).")},
            "sort": {"type": "string", "enum": ["newest", "oldest"], "description": (
                "Discovery only. Temporal bias on top of FTS5 ranking: omit for relevance-only, 'newest' for "
                "\"where did we leave X\", 'oldest' for \"how did X start\".")},
            "detail": {"type": "string", "enum": ["adaptive", "full"], "default": "adaptive", "description": (
                "Discovery only. 'adaptive' hydrates the top result with a message window and returns only the "
                "anchor message for lower results; 'full' returns a window for every result.")},
            "after": {"type": "string", "description": (
                "Discovery/browse. Inclusive lower bound on session start. ISO date/datetime or relative duration "
                "(7d, 24h, 2w = within the last N). Use only when the user names a time frame.")},
            "before": {"type": "string", "description": (
                "Discovery/browse. Exclusive upper bound on session start. ISO date/datetime or relative duration "
                "(7d = older than a week).")},
            "project": {"type": "string", "description": (
                "Optional. Restrict to sessions whose working directory contains this substring "
                "(e.g. a repo name). Omit to search all projects.")},
            "exclude_session_ids": {"type": "array", "items": {"type": "string"}, "description": (
                "Discovery only. Session ids already inspected, so a later query explores instead of repeating the "
                "same hit. Cap 20.")},
            "session_id": {"type": "string", "description": (
                "Scroll/read shape. Session to read inside (from a prior discovery/browse result).")},
            "around_message_id": {"type": "integer", "description": (
                "Scroll shape. Message id to center the window on: match_message_id from a discovery result, or "
                "any id from a prior window.")},
            "window": {"type": "integer", "default": 5, "description": (
                "Scroll only. Messages on each side of the anchor (anchor always included). Clamped to [1, 20].")},
            "role_filter": {"type": "string", "description": (
                "Optional. Comma-separated roles: user, assistant, tool. Default 'user,assistant' (tool output is "
                "usually noise). 'user,assistant,tool' includes tool output; 'tool' searches tool output only.")},
        },
        "required": [],
    },
}


class State:
    conn: Optional[sqlite3.Connection] = None
    has_trigram = False
    last_refresh = 0.0


def _conn() -> sqlite3.Connection:
    if State.conn is None:
        State.conn, State.has_trigram = sessiondb.connect()
    if time.time() - State.last_refresh > REFRESH_INTERVAL_S:  # cheap incremental catch-up (stat per file)
        try:
            sessiondb.index_all(State.conn)
        except sqlite3.Error:
            pass
        State.last_refresh = time.time()
    return State.conn


def _parse_time(value: Optional[str], *, relative_is_lower: bool) -> Optional[str]:
    if not value:
        return None
    m = re.fullmatch(r"\s*(\d+)\s*([hdwm])\s*", value)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"h": timedelta(hours=n), "d": timedelta(days=n), "w": timedelta(weeks=n),
                 "m": timedelta(days=30 * n)}[unit]
        return (datetime.now(timezone.utc) - delta).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"Could not parse time '{value}'. Use ISO (2026-06-01) or relative (7d, 24h, 2w).")
    if dt.tzinfo:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _roles(role_filter: Optional[str]) -> List[str]:
    roles = [r.strip() for r in (role_filter or "user,assistant").split(",") if r.strip()]
    bad = [r for r in roles if r not in ("user", "assistant", "tool")]
    if bad:
        raise ValueError(f"Unknown role(s) {bad}. Use user, assistant, tool.")
    return roles


def _clip(text: Optional[str], n: int) -> str:
    text = text or ""
    return text if len(text) <= n else text[:n] + f"… [+{len(text) - n} chars]"


def _msg(row: sqlite3.Row, n: int = READ_MAX_CONTENT) -> Dict[str, Any]:
    out = {"id": row["id"], "role": row["role"], "timestamp": row["timestamp"], "content": _clip(row["content"], n)}
    if row["tool_name"]:
        out["tool"] = row["tool_name"]
    if row["tool_calls"] and row["role"] == "assistant":
        out["tool_calls"] = _clip(row["tool_calls"], 400)
    return out


def _session_meta(row: sqlite3.Row) -> Dict[str, Any]:
    return {"session_id": row["id"], "title": row["title"], "cwd": row["cwd"], "git_branch": row["git_branch"],
            "started_at": row["started_at"], "last_at": row["last_at"], "message_count": row["message_count"],
            "resume": f"claude --resume {row['id'].split('@')[0]}  (only while the transcript still exists; run it from {row['cwd']})"}


def _session_filters(args: Dict[str, Any], alias: str = "s") -> (str, list):
    where, params = [], []
    after = _parse_time(args.get("after"), relative_is_lower=True)
    before = _parse_time(args.get("before"), relative_is_lower=False)
    if after:
        where.append(f"{alias}.started_at >= ?")
        params.append(after)
    if before:
        where.append(f"{alias}.started_at < ?")
        params.append(before)
    if args.get("project"):
        where.append(f"{alias}.cwd LIKE ?")
        params.append(f"%{args['project']}%")
    return (" AND " + " AND ".join(where)) if where else "", params


def _window(conn, session_id: str, anchor: int, window: int, n: int = READ_MAX_CONTENT,
            roles: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Messages around `anchor`. With `roles`, only those roles (and no empty tool-call-only
    assistant rows unless tool output is requested). The anchor itself is always kept."""
    cond, params = "", []
    if roles:
        cond = f" AND (id=? OR (role IN ({','.join('?' * len(roles))})"
        cond += " AND COALESCE(content,'')!=''))" if "tool" not in roles else "))"
        params = [anchor, *roles]
    before = conn.execute(f"SELECT * FROM messages WHERE session_id=? AND id<?{cond} ORDER BY id DESC LIMIT ?",
                          (session_id, anchor, *params, window)).fetchall()
    after = conn.execute(f"SELECT * FROM messages WHERE session_id=? AND id>=?{cond} ORDER BY id LIMIT ?",
                         (session_id, anchor, *params, window + 1)).fetchall()
    return [_msg(r, n) for r in reversed(before)] + [_msg(r, n) for r in after]


def _fts_candidates(conn, query: str, roles: List[str], extra_where: str, extra_params: list,
                    exclude: List[str]) -> List[sqlite3.Row]:
    role_q = ",".join("?" * len(roles))
    excl = (" AND m.session_id NOT IN (" + ",".join("?" * len(exclude)) + ")") if exclude else ""

    def run(table: str, match: str) -> List[sqlite3.Row]:
        # Without tool output, match message text only (not the tool-call JSON of assistant rows).
        col = f"{table}.content" if "tool" not in roles else table
        sql = (f"SELECT m.id, m.session_id, m.role, bm25({table}) AS rank FROM {table} "
               f"JOIN messages m ON m.id = {table}.rowid JOIN sessions s ON s.id = m.session_id "
               f"WHERE {col} MATCH ? AND m.role IN ({role_q}){excl}{extra_where} ORDER BY rank LIMIT 400")
        return conn.execute(sql, [match, *roles, *exclude, *extra_params]).fetchall()

    tokens = re.findall(r"[\w\-\.]+", query, re.UNICODE)
    quoted_and = " ".join('"' + t.replace('"', '""') + '"' for t in tokens)
    attempts = [("messages_fts", query), ("messages_fts", quoted_and)]
    if State.has_trigram and len(query.strip()) >= 3 and set(roles) <= {"user", "assistant"}:
        attempts.append(("messages_fts_trigram", '"' + query.strip().replace('"', '""') + '"'))
    if len(tokens) > 1:
        attempts.append(("messages_fts", " OR ".join('"' + t.replace('"', '""') + '"' for t in tokens)))
    for table, match in attempts:
        if not match.strip():
            continue
        try:
            rows = run(table, match)
        except sqlite3.OperationalError:
            continue
        if rows:
            return rows
    return []


def discovery(args: Dict[str, Any]) -> Dict[str, Any]:
    conn = _conn()
    limit = max(1, min(int(args.get("limit") or 3), 10))
    roles = _roles(args.get("role_filter"))
    extra_where, extra_params = _session_filters(args)
    exclude = [str(x) for x in (args.get("exclude_session_ids") or [])][:20]
    rows = _fts_candidates(conn, args["query"], roles, extra_where, extra_params, exclude)
    best: Dict[str, sqlite3.Row] = {}
    hits: Dict[str, int] = {}
    for r in rows:
        hits[r["session_id"]] = hits.get(r["session_id"], 0) + 1
        if r["session_id"] not in best:
            best[r["session_id"]] = r
    ranked = sorted(best.values(), key=lambda r: r["rank"])
    sort = args.get("sort")
    if sort in ("newest", "oldest"):
        pool = ranked[: limit * 3]
        meta = {r["session_id"]: conn.execute("SELECT started_at FROM sessions WHERE id=?",
                                              (r["session_id"],)).fetchone()["started_at"] or "" for r in pool}
        ranked = sorted(pool, key=lambda r: meta[r["session_id"]], reverse=(sort == "newest"))
    ranked = ranked[:limit]
    full = args.get("detail") == "full"
    results = []
    for i, r in enumerate(ranked):
        srow = conn.execute("SELECT * FROM sessions WHERE id=?", (r["session_id"],)).fetchone()
        item = _session_meta(srow)
        item["match_message_id"] = r["id"]
        item["matching_messages"] = hits[r["session_id"]]
        if i == 0 or full:
            first = conn.execute("SELECT * FROM messages WHERE session_id=? AND role='user' ORDER BY id LIMIT 1",
                                 (r["session_id"],)).fetchone()
            if first and first["id"] < r["id"] - 3:
                item["session_opening"] = _msg(first, SNIPPET_CHARS)
            item["window"] = _window(conn, r["session_id"], r["id"], 3, 1200, roles)
        else:
            anchor = conn.execute("SELECT * FROM messages WHERE id=?", (r["id"],)).fetchone()
            item["match"] = _msg(anchor, SNIPPET_CHARS)
        results.append(item)
    out: Dict[str, Any] = {"success": True, "shape": "discovery", "query": args["query"], "results": results}
    if not results:
        out["message"] = ("No matching sessions. Try other keywords, a prefix search (term*), include tool output "
                          "(role_filter='user,assistant,tool'), or browse recent sessions (no args).")
    else:
        out["hint"] = ("Scroll deeper with session_id + around_message_id; exclude inspected sessions with "
                       "exclude_session_ids.")
    return out


def scroll(args: Dict[str, Any]) -> Dict[str, Any]:
    conn = _conn()
    sid, anchor = args["session_id"], int(args["around_message_id"])
    window = max(1, min(int(args.get("window") or 5), 20))
    srow = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    if not srow:
        return {"success": False, "error": f"Unknown session_id '{sid}'."}
    msgs = _window(conn, sid, anchor, window, roles=_roles(args.get("role_filter") or "user,assistant,tool"))
    if not msgs:
        return {"success": False, "error": f"No messages around id {anchor} in session '{sid}'."}
    return {"success": True, "shape": "scroll", **_session_meta(srow), "anchor": anchor, "messages": msgs}


def read(args: Dict[str, Any]) -> Dict[str, Any]:
    conn = _conn()
    sid = args["session_id"]
    srow = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    if not srow:
        return {"success": False, "error": f"Unknown session_id '{sid}'."}
    roles = _roles(args.get("role_filter"))
    role_q = ",".join("?" * len(roles))
    empty = " AND COALESCE(content,'')!=''" if "tool" not in roles else ""
    rows = conn.execute(f"SELECT * FROM messages WHERE session_id=? AND role IN ({role_q}){empty} ORDER BY id",
                        (sid, *roles)).fetchall()
    head, tail = 12, 12
    out = {"success": True, "shape": "read", **_session_meta(srow), "shown_roles": roles}
    if len(rows) <= head + tail:
        out["messages"] = [_msg(r) for r in rows]
    else:
        out["messages"] = [_msg(r) for r in rows[:head]]
        out["omitted"] = f"{len(rows) - head - tail} messages omitted, use scroll (around_message_id) to read them"
        out["messages_tail"] = [_msg(r) for r in rows[-tail:]]
    return out


def browse(args: Dict[str, Any]) -> Dict[str, Any]:
    conn = _conn()
    limit = max(1, min(int(args.get("limit") or 10), 50))
    extra_where, params = _session_filters(args)
    rows = conn.execute(f"SELECT * FROM sessions s WHERE s.message_count > 0{extra_where} "
                        f"ORDER BY s.last_at DESC LIMIT ?", (*params, limit)).fetchall()
    return {"success": True, "shape": "browse", "sessions": [_session_meta(r) for r in rows],
            "hint": "Read one with session_id, or search with query."}


def session_search(args: Dict[str, Any]) -> str:
    try:
        if args.get("session_id") and args.get("around_message_id") is not None:
            result = scroll(args)
        elif args.get("query"):
            result = discovery(args)
        elif args.get("session_id"):
            result = read(args)
        else:
            result = browse(args)
    except ValueError as exc:
        result = {"success": False, "error": str(exc)}
    return json.dumps(result, ensure_ascii=False)


def main() -> None:
    server = Server("spickzettel-search", "1.0.0", instructions=(
        "Full-text search over all past Claude Code sessions. When the user references something from a past "
        "conversation or you suspect relevant cross-session context exists, use session_search to recall it "
        "before asking them to repeat themselves."))
    server.tool(SCHEMA, session_search)
    server.run()


if __name__ == "__main__":
    main()
