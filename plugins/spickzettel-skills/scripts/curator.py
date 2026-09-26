#!/usr/bin/env python3
"""Spickzettel skill curator for Claude Code (procedural memory lifecycle).

Only skills the AGENT created are managed (detected when Claude writes a new
.../.claude/skills/<name>/SKILL.md). The user's own skills, plugin skills and pinned
skills are never touched. Lifecycle (defaults as in hermes-agent agent/curator.py):

  active --(14 days without use)--> stale --(30 days without use)--> archived
  any use or edit of a stale skill reactivates it; archiving MOVES the folder to
  ~/.claude/spickzettel/skills/archive/ (never deletes) and can be undone with `restore`.

Usage (hooks):  curator.py hook-pre | hook-post | hook-session-start   (JSON on stdin)
Usage (CLI):    curator.py status | run [--dry-run] | restore <name> | pin <name> | unpin <name>
                curator.py track <path-to-skill-dir> | untrack <name>
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

DAY = 86400
DEFAULTS = {"interval_hours": 168, "stale_after_days": 14, "archive_after_days": 30, "archive_enabled": True}

SKILLS_GUIDANCE = """# Skills are your procedural memory (Spickzettel)
When you work out a non-trivial workflow, fix a tricky problem, or the user corrects how a kind of task should be done, record it as a skill for future reuse. Prefer PATCHING an existing skill that covers the territory over creating a new one.
- Location: `{skills_dir}/<kebab-name>/SKILL.md` (user-level) or `.claude/skills/<name>/SKILL.md` (project-specific).
- Format: YAML frontmatter with `name` and `description` (make the first ~60 chars of the description a self-contained trigger: WHEN to use it), then sections: When to Use, Procedure, Pitfalls, Verification. Put rarely-needed depth in `references/<topic>.md`, starter files in `templates/`, re-runnable actions in `scripts/`.
- Read the current SKILL.md before editing it. Write class-level skills, not one-off session narratives. Do not capture environment failures, unresolved problems or secrets.
- Only skills you create are managed by the curator: unused for {stale} days → stale, for {archive} days → archived (restorable). Never modify skills that came from plugins or that the user wrote themselves unless asked."""


# ─────────────────────────── storage ───────────────────────────

def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()


def base_dir() -> Path:
    env = os.environ.get("SPICKZETTEL_SKILLS_DIR")
    return Path(env).expanduser() if env else claude_home() / "spickzettel" / "skills"


def user_skills_dir() -> Path:
    return claude_home() / "skills"


def load_config() -> Dict[str, Any]:
    cfg = dict(DEFAULTS)
    try:
        data = json.loads((base_dir() / "config.json").read_text(encoding="utf-8"))
        if isinstance(data, dict):
            cfg.update({k: v for k, v in data.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    return cfg


def _state_path() -> Path:
    return base_dir() / "state.json"


def load_state() -> Dict[str, Any]:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("skills", {})
            return data
    except (OSError, ValueError):
        pass
    return {"skills": {}, "last_run": 0}


def save_state(state: Dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".state_", dir=str(path.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


class Locked:
    """Exclusive lock around read-modify-write of state.json (hooks can run concurrently)."""

    def __enter__(self):
        base_dir().mkdir(parents=True, exist_ok=True)
        self.f = open(base_dir() / ".lock", "a+")
        try:
            import fcntl
            fcntl.flock(self.f, fcntl.LOCK_EX)
        except ImportError:
            pass
        return self

    def __exit__(self, *exc):
        self.f.close()


def ledger(event: str, **fields) -> None:
    base_dir().mkdir(parents=True, exist_ok=True)
    rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event, **fields}
    with open(base_dir() / "curator_ledger.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _skill_dir_from_file(fp: str) -> Optional[Path]:
    if not fp:
        return None
    p = Path(os.path.expanduser(fp))
    if p.name != "SKILL.md":
        return None
    try:
        p = p.resolve()
    except OSError:
        return None
    skills_root = p.parent.parent
    is_skills_root = skills_root.name == "skills" and (
        skills_root.parent.name == ".claude" or skills_root == user_skills_dir().resolve())
    if not is_skills_root:
        return None
    return p.parent


def _frontmatter_name(skill_dir: Path) -> Optional[str]:
    try:
        text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.match(r"---\s*\n(.*?)\n---", text, re.S)
    if m:
        nm = re.search(r"^name:\s*['\"]?([^'\"\n]+)", m.group(1), re.M)
        if nm:
            return nm.group(1).strip()
    return None


# ─────────────────────────── tracking ───────────────────────────

def _touch(state: Dict[str, Any], key: str, *, use: bool) -> bool:
    rec = state["skills"].get(key)
    if not rec:
        return False
    now = time.time()
    rec["last_activity"] = now
    if use:
        rec["use_count"] = int(rec.get("use_count", 0)) + 1
        rec["last_used"] = now
    if rec.get("status") == "stale":
        rec["status"] = "active"
        ledger("reactivated", name=rec["name"], path=key)
    return True


def hook_pre(data: Dict[str, Any]) -> None:
    """PreToolUse Write|Edit|MultiEdit: a SKILL.md that does not exist yet is being created by the agent."""
    skill_dir = _skill_dir_from_file((data.get("tool_input") or {}).get("file_path") or "")
    if not skill_dir or (skill_dir / "SKILL.md").exists():
        return
    with Locked():
        state = load_state()
        key = str(skill_dir)
        if key not in state["skills"]:
            now = time.time()
            state["skills"][key] = {"name": skill_dir.name, "origin": "agent", "created_at": now,
                                    "last_activity": now, "use_count": 0, "status": "active", "pinned": False,
                                    "session_id": data.get("session_id")}
            save_state(state)
            ledger("created", name=skill_dir.name, path=key, session_id=data.get("session_id"))


def hook_post(data: Dict[str, Any]) -> None:
    """PostToolUse Skill|Read|Write|Edit|MultiEdit: record use / activity of tracked skills."""
    tool, tin = data.get("tool_name"), data.get("tool_input") or {}
    with Locked():
        state = load_state()
        changed = False
        if tool == "Skill":
            raw = str(tin.get("skill") or tin.get("name") or tin.get("command") or "").strip().lstrip("/")
            name = raw.split(":")[-1].split()[0] if raw else ""
            if name:
                for key, rec in state["skills"].items():
                    if name in (rec["name"], rec.get("frontmatter_name")):
                        changed |= _touch(state, key, use=True)
        else:
            skill_dir = _skill_dir_from_file(tin.get("file_path") or "")
            if skill_dir:
                changed |= _touch(state, str(skill_dir), use=(tool == "Read"))
                rec = state["skills"].get(str(skill_dir))
                if rec is not None:
                    rec["frontmatter_name"] = _frontmatter_name(skill_dir)
        if changed:
            save_state(state)


# ─────────────────────────── curator pass ───────────────────────────

def run_pass(*, dry_run: bool = False, force: bool = False) -> Dict[str, Any]:
    cfg = load_config()
    now = time.time()
    stale_cut = now - cfg["stale_after_days"] * DAY
    archive_cut = now - cfg["archive_after_days"] * DAY
    report = {"checked": 0, "marked_stale": [], "archived": [], "reactivated": [], "dropped": [], "ran": False}
    with Locked():
        state = load_state()
        if not force and now - float(state.get("last_run") or 0) < cfg["interval_hours"] * 3600:
            return report
        report["ran"] = True
        for key, rec in list(state["skills"].items()):
            if rec.get("status") == "archived":
                continue
            skill_dir = Path(key)
            if not (skill_dir / "SKILL.md").exists():
                report["dropped"].append(rec["name"])
                if not dry_run:
                    del state["skills"][key]
                    ledger("dropped", name=rec["name"], path=key, reason="SKILL.md no longer exists")
                continue
            report["checked"] += 1
            if rec.get("pinned"):
                continue
            anchor = float(rec.get("last_activity") or rec.get("created_at") or 0)
            if int(rec.get("use_count", 0)) == 0 and anchor > stale_cut:
                continue  # never-used but young: absence of evidence is not staleness
            if anchor <= archive_cut and cfg["archive_enabled"]:
                report["archived"].append(rec["name"])
                if not dry_run:
                    dest = base_dir() / "archive" / f"{rec['name']}--{datetime.now().strftime('%Y%m%d-%H%M%S')}"
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(skill_dir), str(dest))
                    rec.update(status="archived", archived_at=now, archive_path=str(dest))
                    ledger("archived", name=rec["name"], path=key, archive_path=str(dest))
            elif anchor <= stale_cut and rec.get("status") != "stale":
                report["marked_stale"].append(rec["name"])
                if not dry_run:
                    rec["status"] = "stale"
                    ledger("marked_stale", name=rec["name"], path=key)
            elif anchor > stale_cut and rec.get("status") == "stale":
                report["reactivated"].append(rec["name"])
                if not dry_run:
                    rec["status"] = "active"
        if not dry_run:
            state["last_run"] = now
            save_state(state)
    return report


def hook_session_start(data: Dict[str, Any]) -> None:
    cfg = load_config()
    report = run_pass()
    state = load_state()
    ctx = [SKILLS_GUIDANCE.format(skills_dir=user_skills_dir(), stale=cfg["stale_after_days"],
                                  archive=cfg["archive_after_days"])]
    stale = [r for r in state["skills"].values() if r.get("status") == "stale" and not r.get("pinned")]
    if stale:
        items = []
        for r in stale:
            left = int((float(r.get("last_activity") or r.get("created_at") or 0)
                        + cfg["archive_after_days"] * DAY - time.time()) / DAY)
            items.append(f"{r['name']} (archived in ~{max(left, 0)} days unless used)")
        ctx.append("Stale agent-created skills: " + ", ".join(items) + ". If one overlaps with another skill, "
                   "consider merging it into that skill.")
    out: Dict[str, Any] = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "\n\n".join(ctx)}}
    if report["ran"] and (report["archived"] or report["marked_stale"]):
        msg = []
        if report["archived"]:
            msg.append("archived: " + ", ".join(report["archived"]))
        if report["marked_stale"]:
            msg.append("marked stale: " + ", ".join(report["marked_stale"]))
        out["systemMessage"] = "Skill curator: " + "; ".join(msg) + " (restore with: spickzettel-curator restore <name>)"
    print(json.dumps(out, ensure_ascii=False))


# ─────────────────────────── CLI ───────────────────────────

def _fmt_age(ts: Optional[float]) -> str:
    if not ts:
        return "never"
    d = (time.time() - float(ts)) / DAY
    return f"{d:.0f}d ago" if d >= 1 else f"{d * 24:.0f}h ago"


def cmd_status() -> None:
    cfg, state = load_config(), load_state()
    print(f"Curator: stale after {cfg['stale_after_days']}d, archive after {cfg['archive_after_days']}d "
          f"(archiving {'on' if cfg['archive_enabled'] else 'off'}), runs every {cfg['interval_hours']}h; "
          f"last run {_fmt_age(state.get('last_run'))}.")
    if not state["skills"]:
        print("No agent-created skills tracked yet.")
        return
    print(f"{'NAME':28} {'STATUS':9} {'USES':>4}  {'LAST ACTIVITY':14} PATH")
    for key, r in sorted(state["skills"].items(), key=lambda kv: kv[1]["name"]):
        status = r.get("status", "active") + ("*" if r.get("pinned") else "")
        print(f"{r['name'][:28]:28} {status:9} {int(r.get('use_count', 0)):>4}  "
              f"{_fmt_age(r.get('last_activity')):14} {r.get('archive_path') or key}")
    print("(* = pinned, never archived)")


def _find(state, name: str, *, status: Optional[str] = None):
    hits = [(k, r) for k, r in state["skills"].items()
            if r["name"] == name and (status is None or r.get("status") == status)]
    return hits[-1] if hits else (None, None)


def cmd_restore(name: str) -> int:
    with Locked():
        state = load_state()
        key, rec = _find(state, name, status="archived")
        if not rec:
            print(f"No archived skill named '{name}'.")
            return 1
        if Path(key).exists():
            print(f"Cannot restore: {key} already exists.")
            return 1
        Path(key).parent.mkdir(parents=True, exist_ok=True)
        shutil.move(rec["archive_path"], key)
        rec.update(status="active", last_activity=time.time())
        rec.pop("archive_path", None)
        rec.pop("archived_at", None)
        save_state(state)
        ledger("restored", name=name, path=key)
    print(f"Restored '{name}' to {key}.")
    return 0


def cmd_pin(name: str, pinned: bool) -> int:
    with Locked():
        state = load_state()
        key, rec = _find(state, name)
        if not rec:
            print(f"No tracked skill named '{name}'.")
            return 1
        rec["pinned"] = pinned
        save_state(state)
    print(f"{'Pinned' if pinned else 'Unpinned'} '{name}'.")
    return 0


def cmd_track(path: str) -> int:
    skill_dir = Path(os.path.expanduser(path)).resolve()
    if skill_dir.name == "SKILL.md":
        skill_dir = skill_dir.parent
    if not (skill_dir / "SKILL.md").exists():
        print(f"No SKILL.md in {skill_dir}.")
        return 1
    with Locked():
        state = load_state()
        now = time.time()
        state["skills"].setdefault(str(skill_dir), {
            "name": skill_dir.name, "origin": "manual-track", "created_at": now, "last_activity": now,
            "use_count": 0, "status": "active", "pinned": False, "frontmatter_name": _frontmatter_name(skill_dir)})
        save_state(state)
        ledger("tracked", name=skill_dir.name, path=str(skill_dir))
    print(f"Now tracking '{skill_dir.name}'.")
    return 0


def cmd_untrack(name: str) -> int:
    with Locked():
        state = load_state()
        key, rec = _find(state, name)
        if not rec:
            print(f"No tracked skill named '{name}'.")
            return 1
        del state["skills"][key]
        save_state(state)
        ledger("untracked", name=name, path=key)
    print(f"Stopped tracking '{name}' (files untouched).")
    return 0


def main(argv: List[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd.startswith("hook-"):
        try:
            data = json.load(sys.stdin)
        except ValueError:
            data = {}
        try:
            {"hook-pre": hook_pre, "hook-post": hook_post, "hook-session-start": hook_session_start}[cmd](data)
        except Exception as exc:  # never disturb the session
            print(f"spickzettel-skills: {cmd} failed: {exc}", file=sys.stderr)
        return 0
    if cmd == "status":
        cmd_status()
        return 0
    if cmd == "run":
        dry = "--dry-run" in argv
        rep = run_pass(dry_run=dry, force=True)
        print(("DRY RUN, nothing changed. " if dry else "") + json.dumps(
            {k: v for k, v in rep.items() if k != "ran"}, ensure_ascii=False))
        return 0
    if cmd in ("restore", "pin", "unpin", "track", "untrack") and len(argv) < 3:
        print(f"usage: spickzettel-curator {cmd} <name{'|path' if cmd == 'track' else ''}>")
        return 2
    if cmd == "restore":
        return cmd_restore(argv[2])
    if cmd in ("pin", "unpin"):
        return cmd_pin(argv[2], cmd == "pin")
    if cmd == "track":
        return cmd_track(argv[2])
    if cmd == "untrack":
        return cmd_untrack(argv[2])
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
