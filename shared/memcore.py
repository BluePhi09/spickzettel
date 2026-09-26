"""Spickzettel: bounded, curated memory (MEMORY.md / USER.md) for Claude Code.

Ported from NousResearch/hermes-agent (tools/memory_tool_store.py and
tools/threat_patterns.py), MIT License, Copyright (c) 2025 Nous Research.
Adapted to run standalone (stdlib only) inside Claude Code plugins.

THIS FILE IS SHARED: the canonical copy lives in shared/memcore.py and is copied
into each plugin by scripts/sync-shared.sh. Edit the shared copy only.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import unicodedata
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import fcntl  # type: ignore
except ImportError:  # Windows
    fcntl = None  # type: ignore
    try:
        import msvcrt  # type: ignore
    except ImportError:
        msvcrt = None  # type: ignore
else:
    msvcrt = None  # type: ignore

ENTRY_DELIMITER = "\n§\n"
MEMORY_BLOCK_HEADERS = {"memory": "MEMORY (your personal notes)", "user": "USER PROFILE (who the user is)"}
DEFAULTS = {"memory_char_limit": 2200, "user_char_limit": 1375, "memory_enabled": True, "user_profile_enabled": True}


# ─────────────────────────── paths & config ───────────────────────────

def memory_dir() -> Path:
    """~/.claude/spickzettel/memory, overridable with SPICKZETTEL_MEMORY_DIR."""
    env = os.environ.get("SPICKZETTEL_MEMORY_DIR")
    if env:
        return Path(env).expanduser()
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()
    return base / "spickzettel" / "memory"


def load_config() -> Dict[str, Any]:
    cfg = dict(DEFAULTS)
    path = memory_dir() / "config.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            cfg.update({k: v for k, v in data.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    for key, env in (("memory_char_limit", "SPICKZETTEL_MEMORY_CHAR_LIMIT"), ("user_char_limit", "SPICKZETTEL_USER_CHAR_LIMIT")):
        if os.environ.get(env, "").isdigit():
            cfg[key] = int(os.environ[env])
    return cfg


# ─────────────────────────── threat scanning ───────────────────────────
# Verbatim pattern set from hermes-agent tools/threat_patterns.py (strict scope is used
# for memory writes). ".hermes" paths extended with ".claude" equivalents.

MAX_SCAN_CHARS = 65_536
_FILLER = r"(?:\w+\s+){0,8}"
_SECRET_VAR = r"\$\{?\w*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)S?\b"
_MODIFY = r"(update|modify|edit|write|change|append|add\s+to)\s+[^\n]{0,2048}"
_PATTERNS: List[Tuple[str, str, str]] = [
    (rf'ignore\s+{_FILLER}(previous|all|above|prior)\s+{_FILLER}instructions', "prompt_injection", "all"),
    (r'system\s+prompt\s+override', "sys_prompt_override", "all"),
    (rf'disregard\s+{_FILLER}(your|all|any)\s+{_FILLER}(instructions|rules|guidelines)', "disregard_rules", "all"),
    (rf'act\s+as\s+(if|though)\s+{_FILLER}you\s+{_FILLER}(have\s+no|don\'t\s+have)\s+{_FILLER}(restrictions|limits|rules)', "bypass_restrictions", "all"),
    (r'<!--[^>]{0,512}(?:ignore|override|system|secret|hidden)[^>]{0,512}-->', "html_comment_injection", "all"),
    (r'<\s*div\s+style\s*=\s*["\'][^>]{0,2048}display\s*:\s*none', "hidden_div", "all"),
    (r"translate\s+[^\n]{0,512}\s+into\s+\w+(?:[\s-]+\w+){0,2}\s+and\s+(execute|run|eval)\b", "translate_execute", "all"),
    (rf'do\s+not\s+{_FILLER}tell\s+{_FILLER}the\s+user', "deception_hide", "all"),
    (rf'you\s+are\s+{_FILLER}now\s+(?:a|an|the)\s+', "role_hijack", "context"),
    (rf'pretend\s+{_FILLER}(you\s+are|to\s+be)\s+', "role_pretend", "context"),
    (rf'output\s+{_FILLER}(system|initial)\s+prompt', "leak_system_prompt", "context"),
    (rf'(respond|answer|reply)\s+without\s+{_FILLER}(restrictions|limitations|filters|safety)', "remove_filters", "context"),
    (rf'you\s+have\s+been\s+{_FILLER}(updated|upgraded|patched)\s+to', "fake_update", "context"),
    (r'\bname\s+yourself\s+\w+', "identity_override", "context"),
    (r'register\s+(as\s+)?a?\s*node', "c2_node_registration", "context"),
    (r'(heartbeat|beacon|check[\s\-]?in)\s+(to|with)\s+', "c2_heartbeat", "context"),
    (r'pull\s+(down\s+)?(?:new\s+)?task(?:ing|s)?\b', "c2_task_pull", "context"),
    (r'connect\s+to\s+the\s+network\b', "c2_network_connect", "context"),
    (r'you\s+must\s+(?:\w+\s+){0,3}(register|connect|report|beacon)\b', "forced_action", "context"),
    (r'only\s+use\s+one[\s\-]?liners?\b', "anti_forensic_oneliner", "context"),
    (rf'never\s+{_FILLER}(?:create|write)\s+{_FILLER}(?:script|file)\s+{_FILLER}disk', "anti_forensic_disk", "context"),
    (r'unset\s+\w*(?:CLAUDE|CODEX|HERMES|AGENT|OPENAI|ANTHROPIC)\w*', "env_var_unset_agent", "context"),
    (r'\b(?:cobalt\s*strike|sliver|havoc|mythic|metasploit|brainworm)\b', "known_c2_framework", "context"),
    (r'\bc2\s+(?:server|channel|infrastructure|beacon)\b', "c2_explicit", "context"),
    (r'\bcommand\s+and\s+control\b', "c2_explicit_long", "context"),
    (rf'curl\s+[^\n]{{0,2048}}{_SECRET_VAR}', "exfil_curl", "all"),
    (rf'wget\s+[^\n]{{0,2048}}{_SECRET_VAR}', "exfil_wget", "all"),
    (r'cat\s+[^\n]{0,2048}(\.env|credentials|\.netrc|\.pgpass|\.npmrc|\.pypirc)', "read_secrets", "all"),
    (r'(send|post|upload|transmit)\s+[^\n]{0,2048}\s+(to|at)\s+https?://', "send_to_url", "strict"),
    (rf'(include|output|print|share)\s+{_FILLER}(conversation|chat\s+history|previous\s+messages|full\s+context|entire\s+context)', "context_exfil", "strict"),
    (r'authorized_keys', "ssh_backdoor", "strict"),
    (r'(?:\b(?:echo|cat|cp|mv|dd|tee|install|printf|rsync|scp|ln|append|add|write'
     r'|sed|chmod|chown|truncate|rm|touch|curl|wget|git)\b|\bopen\s*\(|>>?)'
     r'[^\n]{0,512}(?:\$HOME/\.ssh|~/\.ssh)', "ssh_access", "strict"),
    (r'\$HOME/\.(?:hermes|claude)/\.env|\~/\.(?:hermes|claude)/\.env', "agent_env", "strict"),
    (rf'{_MODIFY}(?:AGENTS\.md|CLAUDE\.md|\.cursorrules|\.clinerules)', "agent_config_mod", "strict"),
    (rf'{_MODIFY}\.claude/(settings(?:\.local)?\.json|spickzettel/memory/config\.json)', "claude_config_mod", "strict"),
    (r'(?:api[_-]?key|token|secret|password)\s*[=:]\s*["\']'
     r'(?!(?-i:[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)["\'])'
     r'[A-Za-z0-9+/=_-]{20,}', "hardcoded_secret", "strict"),
]
INVISIBLE_CHARS = frozenset("​‌‍⁠⁢⁣⁤﻿"
                            "‪‫‬‭‮⁦⁧⁨⁩")
_SCOPE_SETS = {"all": ("all", "context", "strict"), "context": ("context", "strict"), "strict": ("strict",)}
_COMPILED: Dict[str, List[Tuple[re.Pattern, str]]] = {"all": [], "context": [], "strict": []}
for _p, _pid, _scope in _PATTERNS:
    for _s in _SCOPE_SETS[_scope]:
        _COMPILED[_s].append((re.compile(_p, re.IGNORECASE), _pid))


def scan_for_threats(content: str, scope: str = "strict") -> List[str]:
    if not content:
        return []
    content = content[:MAX_SCAN_CHARS]
    findings = [f"invisible_unicode_U+{ord(ch):04X}" for ch in set(content) & INVISIBLE_CHARS]
    normalised = unicodedata.normalize("NFKC", content)
    findings.extend(pid for rx, pid in _COMPILED[scope] if rx.search(normalised))
    return findings


def first_threat_message(content: str, scope: str = "strict") -> Optional[str]:
    findings = scan_for_threats(content, scope)
    if not findings:
        return None
    pid = findings[0]
    if pid.startswith("invisible_unicode_"):
        return f"Blocked: content contains invisible unicode character {pid.replace('invisible_unicode_', '')} (possible injection)."
    return (f"Blocked: content matches threat pattern '{pid}'. Content is injected into the system prompt "
            f"and must not contain injection or exfiltration payloads.")


# ─────────────────────────── helpers ───────────────────────────

def _error(message: str, **extra) -> Dict[str, Any]:
    return {"success": False, "error": message, **extra}


def parse_entries(raw: str) -> List[str]:
    return [e for e in (x.strip() for x in raw.split(ENTRY_DELIMITER)) if e]


def read_raw_checked(path: Path) -> Tuple[str, bool]:
    if not path.exists():
        return "", True
    try:
        return path.read_text(encoding="utf-8-sig"), True
    except (OSError, UnicodeDecodeError):
        return "", False


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".mem_", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp)
        raise


def _find_unique_match(entries: List[str], old_text: str) -> Tuple[Optional[int], bool]:
    exact = [i for i, e in enumerate(entries) if e == old_text]
    matches = exact if exact else [i for i, e in enumerate(entries) if old_text in e]
    if len({entries[i] for i in matches}) > 1:
        return None, True
    return (matches[0] if matches else None), False


# ─────────────────────────── store ───────────────────────────

class MemoryStore:
    """Bounded curated memory with file persistence (semantics of the hermes-agent memory store)."""

    MAX_CONSOLIDATION_FAILURES_PER_TURN = 3

    def __init__(self, cfg: Optional[Dict[str, Any]] = None, directory: Optional[Path] = None):
        cfg = cfg or load_config()
        self.dir = directory or memory_dir()
        self.memory_char_limit = int(cfg["memory_char_limit"])
        self.user_char_limit = int(cfg["user_char_limit"])
        self.memory_enabled = bool(cfg["memory_enabled"])
        self.user_profile_enabled = bool(cfg["user_profile_enabled"])
        self.memory_entries: List[str] = []
        self.user_entries: List[str] = []
        self._consolidation_failures = 0

    # basics
    def target_enabled(self, target: str) -> bool:
        return self.user_profile_enabled if target == "user" else self.memory_enabled

    def path_for(self, target: str) -> Path:
        return self.dir / ("USER.md" if target == "user" else "MEMORY.md")

    def entries_for(self, target: str) -> List[str]:
        return self.user_entries if target == "user" else self.memory_entries

    def _set_entries(self, target: str, entries: List[str]) -> None:
        setattr(self, "user_entries" if target == "user" else "memory_entries", entries)

    def char_limit(self, target: str) -> int:
        return self.user_char_limit if target == "user" else self.memory_char_limit

    def char_count(self, target: str) -> int:
        return len(ENTRY_DELIMITER.join(self.entries_for(target)))

    def usage(self, target: str) -> str:
        return f"{self.char_count(target):,}/{self.char_limit(target):,}"

    def usage_pct(self, target: str, current: int) -> str:
        limit = self.char_limit(target)
        return f"{min(100, int(current / limit * 100)) if limit > 0 else 0}%, {current:,}/{limit:,} chars"

    def reset_consolidation_failures(self) -> None:
        self._consolidation_failures = 0

    def _consolidation_failure(self, response: Dict[str, Any]) -> Dict[str, Any]:
        self._consolidation_failures += 1
        if self._consolidation_failures <= self.MAX_CONSOLIDATION_FAILURES_PER_TURN:
            return response
        return {"success": False, "done": True, "error": (
            f"Memory consolidation failed {self._consolidation_failures} times this turn. Stop retrying "
            "memory calls. Leave memory unchanged for now and continue with your reply to the user. "
            "The fact can be saved in a later turn.")}

    # load / render (frozen snapshot)
    def load_from_disk(self) -> None:
        for target in ("memory", "user"):
            self._set_entries(target, list(dict.fromkeys(parse_entries(read_raw_checked(self.path_for(target))[0]))))

    def render_block(self, target: str) -> str:
        entries = self.entries_for(target)
        if not entries:
            return ""
        name = self.path_for(target).name
        safe = []
        for e in entries:
            findings = scan_for_threats(e) if not e.startswith("[BLOCKED:") else None
            safe.append(e if not findings else
                        f"[BLOCKED: {name} entry contained threat pattern(s): {', '.join(findings)}. "
                        f"Removed from context; remove the original entry from {name}.]")
        content, sep = ENTRY_DELIMITER.join(safe), "═" * 46
        over = ""
        if self.char_count(target) > self.char_limit(target):
            over = ", OVER LIMIT: consolidate before adding"
        return (f"{sep}\n{MEMORY_BLOCK_HEADERS[target]} [{self.usage_pct(target, self.char_count(target))}{over}]\n"
                f"{sep}\n{content}")

    # locking + mutate
    @contextmanager
    def _file_lock(self, path: Path):
        lock_path = path.with_suffix(path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        if fcntl is None and msvcrt is None:
            yield
            return
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        raw_fd = os.open(lock_path, flags, 0o600)
        with suppress(OSError, AttributeError):
            os.fchmod(raw_fd, 0o600)
        with os.fdopen(raw_fd, "r+", encoding="utf-8") as fd:
            if fcntl:
                fcntl.flock(fd, fcntl.LOCK_EX)
            else:
                msvcrt.locking(fd.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                with suppress(OSError):
                    if fcntl:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    else:
                        fd.seek(0)
                        msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)

    def _detect_external_drift(self, target: str, raw: str) -> Optional[str]:
        parsed = parse_entries(raw)
        if not raw.strip() or (raw.strip() == ENTRY_DELIMITER.join(parsed)
                               and max(map(len, parsed), default=0) <= self.char_limit(target)):
            return None
        path = self.path_for(target)
        bak = path.with_suffix(path.suffix + f".bak.{int(time.time())}")
        try:
            bak.write_text(raw, encoding="utf-8")
        except OSError:
            return str(bak) + " (BACKUP FAILED, file unchanged on disk)"
        return str(bak)

    def _mutate(self, target: str, mutate, *, skip_drift: bool = False) -> Dict[str, Any]:
        path = self.path_for(target)
        with self._file_lock(path):
            raw, ok = read_raw_checked(path)
            if not ok:
                return _error(f"Refusing to write {path.name}: the file exists but could not be read right now "
                              f"(locked, permissions, or invalid encoding). Nothing was changed; retry in a moment.")
            bak = None if skip_drift else self._detect_external_drift(target, raw)
            self._set_entries(target, list(dict.fromkeys(parse_entries(raw))))
            if bak:
                return _error(
                    f"Refusing to write {path.name}: file on disk has content that wouldn't round-trip through the "
                    f"memory tool (manual edit, shell append or concurrent session). A snapshot was saved to {bak}. "
                    f"Rewrite the file as a clean §-delimited list of entries (a line containing only § between "
                    f"entries), then retry.", drift_backup=bak)
            result = mutate(self.entries_for(target), self.char_limit(target))
            if isinstance(result, dict):
                return result
            self._set_entries(target, result[0])
            atomic_write_text(path, ENTRY_DELIMITER.join(result[0]))
            return self._success(target, result[1], **(result[2] if len(result) > 2 else {}))

    def _success(self, target: str, message: str = "", **extra) -> Dict[str, Any]:
        self._consolidation_failures = 0
        return {"success": True, "done": True, "target": target,
                "usage": self.usage_pct(target, self.char_count(target)),
                "entry_count": len(self.entries_for(target)), **({"message": message} if message else {}),
                **extra, "note": "Write saved. This update is complete; do not repeat it. "
                                 "It becomes visible in context from the next session or after compaction."}

    def _failure_with_entries(self, target: str, message: str) -> Dict[str, Any]:
        return self._consolidation_failure(_error(message, current_entries=self.entries_for(target),
                                                  usage=self.usage(target)))

    def _batch_failure(self, target: str, message: str) -> Dict[str, Any]:
        return self._consolidation_failure(
            _error(message + " No operations were applied (batch is all-or-nothing).", usage=self.usage(target)))

    # operations
    def add(self, target: str, content: str) -> Dict[str, Any]:
        content = (content or "").strip()
        if not content:
            return _error("Content cannot be empty.")
        if scan := first_threat_message(content):
            return _error(scan)

        def _add(entries, limit):
            if content in entries:
                return self._success(target, "Entry already exists (no duplicate added).")
            if len(ENTRY_DELIMITER.join(entries + [content])) > limit:
                return self._failure_with_entries(target, (
                    f"Memory at {self.char_count(target):,}/{limit:,} chars. Adding this entry ({len(content)} chars) "
                    f"would exceed the limit. Consolidate now: use 'replace' to merge overlapping entries into shorter "
                    f"ones or 'remove' stale or less important entries (see current_entries below), then retry this "
                    f"add, all in this turn. Prefer ONE batch call via 'operations'."))
            return entries + [content], "Entry added."
        return self._mutate(target, _add, skip_drift=True)

    def _locate(self, entries: List[str], old_text: str, verb: str):
        idx, ambiguous = _find_unique_match(entries, old_text)
        if ambiguous:
            return _error(f"Multiple entries matched '{old_text}'. Be more specific.",
                          matches=[e[:80] + ("..." if len(e) > 80 else "") for e in entries if old_text in e])
        if idx is None:
            return self._consolidation_failure(_error(
                f"No entry matched '{old_text}'. Check current_entries below and retry with the exact text of the "
                f"entry you want to {verb}.", current_entries=entries))
        return idx

    def replace(self, target: str, old_text: str, new_content: str) -> Dict[str, Any]:
        new_content, old_text = (new_content or "").strip(), (old_text or "").strip()
        if not old_text:
            return _error("old_text cannot be empty.")
        if not new_content:
            return _error("new_content cannot be empty. Use 'remove' to delete entries.")
        if scan := first_threat_message(new_content):
            return _error(scan)

        def _apply(entries, limit):
            idx = self._locate(entries, old_text, "replace")
            if isinstance(idx, dict):
                return idx
            replaced = entries[:idx] + [new_content] + entries[idx + 1:]
            total = len(ENTRY_DELIMITER.join(replaced))
            if total > limit:
                return self._failure_with_entries(target, (
                    f"Replacement would put memory at {total:,}/{limit:,} chars. Shorten the new content, or 'remove' "
                    f"other stale entries to make room (see current_entries below), then retry, all in this turn."))
            return replaced, "Entry replaced.", {"replaced_entry": entries[idx]}
        return self._mutate(target, _apply)

    def remove(self, target: str, old_text: str) -> Dict[str, Any]:
        old_text = (old_text or "").strip()
        if not old_text:
            return _error("old_text cannot be empty.")

        def _apply(entries, limit):
            idx = self._locate(entries, old_text, "remove")
            if isinstance(idx, dict):
                return idx
            return entries[:idx] + entries[idx + 1:], "Entry removed.", {"removed_entry": entries[idx]}
        return self._mutate(target, _apply)

    def apply_batch(self, target: str, operations: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not operations:
            return _error("operations list is empty.")
        ops = [op or {} for op in operations]
        for i, op in enumerate(ops):
            text = op.get("content") or op.get("new_text")
            if op.get("action") in {"add", "replace"} and text and (scan := first_threat_message(text)):
                return _error(f"Operation {i + 1}: {scan}")

        def _apply(entries, limit):
            working = list(entries)
            replaced, removed = {}, {}
            for i, op in enumerate(ops, 1):
                act = op.get("action")
                content = (op.get("content") or op.get("new_text") or "").strip()
                old = (op.get("old_text") or "").strip()
                pos = f"Operation {i} ({act or 'unknown'})"
                if act == "add":
                    if not content:
                        return self._batch_failure(target, f"{pos}: content is required.")
                    if content not in working:
                        working.append(content)
                    continue
                if act not in ("replace", "remove"):
                    return self._batch_failure(target, f"{pos}: unknown action. Use add, replace, or remove.")
                if not old:
                    return self._batch_failure(target, f"{pos}: old_text is required.")
                if act == "replace" and not content:
                    return self._batch_failure(target, f"{pos}: content is required (use action='remove' to delete).")
                idx, ambiguous = _find_unique_match(working, old)
                if ambiguous:
                    return self._batch_failure(target, f"{pos}: '{old}' matched multiple distinct entries -- be more specific.")
                if idx is None:
                    return self._batch_failure(target, f"{pos}: no entry matched '{old}'.")
                (replaced if act == "replace" else removed)[i] = working[idx]
                working[idx:idx + 1] = [content] if act == "replace" else []
            if entries and not working:
                return self._batch_failure(target, (
                    f"Refusing to empty {self.path_for(target).name}: this batch would remove every entry from a "
                    f"previously non-empty store. Keep at least one entry; use single remove() calls to delete the "
                    f"final entry deliberately."))
            total = len(ENTRY_DELIMITER.join(working))
            if total > limit:
                return self._batch_failure(target, (
                    f"After applying all {len(ops)} operations, memory would be at {total:,}/{limit:,} chars -- over "
                    f"the limit. Remove or shorten more entries in the same batch, then retry."))
            extra = {}
            if replaced:
                extra["replaced_entries"] = replaced
            if removed:
                extra["removed_entries"] = removed
            return working, f"Applied {len(ops)} operation(s).", extra
        return self._mutate(target, _apply)


# ─────────────────────────── validation of a hand-edited file ───────────────────────────

def validate_file(target: str, store: Optional[MemoryStore] = None) -> List[str]:
    """Problems with MEMORY.md/USER.md as they are on disk (used after direct edits)."""
    store = store or MemoryStore()
    path = store.path_for(target)
    raw, ok = read_raw_checked(path)
    if not ok:
        return [f"{path.name} could not be read (encoding or permission problem)."]
    entries = parse_entries(raw)
    problems = []
    total = len(ENTRY_DELIMITER.join(entries))
    if total > store.char_limit(target):
        problems.append(f"{path.name} is at {total:,}/{store.char_limit(target):,} chars, over the limit. "
                        f"Consolidate: merge overlapping entries and remove stale ones until it fits.")
    if len(entries) != len(set(entries)):
        problems.append(f"{path.name} contains duplicate entries; remove the duplicates.")
    for e in entries:
        if msg := first_threat_message(e):
            problems.append(f"{path.name} entry '{e[:60]}…': {msg} Remove or rewrite it.")
    return problems
