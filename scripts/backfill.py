"""
Backfill Claude Code transcripts into the vault through the live pipeline.

Walks ~/.claude/projects/*/*.jsonl (top-level session transcripts only; the
subagents/ files underneath are children and are skipped), applies the same
guards as ~/.local/bin/session-capture-hook, and flushes each session onto
its own day with `flush.py --date`. Then compiles every daily log that the
compile state does not already know (compile.py --file, one day at a time).

Usage:
    uv run python backfill.py --dry-run                 # list what would run
    uv run python backfill.py --days 2026-10-05,2026-10-12
    uv run python backfill.py                           # everything, oldest first
    uv run python backfill.py --phase compile           # only the compile step

Idempotent: a session already present in its day's log (the
`<!-- session: <id> -->` marker flush.py writes) or recorded as done in
backfill-state.json is skipped; a compiled day is skipped by compile.py's own
hash check. On a usage-limit exit (3) the driver sleeps until the window
resets and retries the same item, so an exhausted 5-hour window pauses the
run instead of breaking it. Progress goes to backfill.log in the state dir.
"""

from __future__ import annotations

import argparse
import fcntl
import glob
import json
import os
import re
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from config import DAILY_DIR, STATE_DIR, STATE_FILE, TIMEZONE, ensure_state_dir

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
HOOKS_DIR = ROOT / "hooks"
sys.path.insert(0, str(HOOKS_DIR))
from session_end_extract import extract_conversation_context  # noqa: E402

PROJECTS_DIR = Path(os.environ.get("CLAUDE_PROJECTS_DIR") or Path.home() / ".claude" / "projects")
BACKFILL_LOG = STATE_DIR / "backfill.log"
BACKFILL_STATE = STATE_DIR / "backfill-state.json"
LOCK_FILE = STATE_DIR / "backfill.lock"
HOST = socket.gethostname().split(".")[0].lower() or "unknown-host"
TZ = ZoneInfo(TIMEZONE)
WORKTREE_PATH_RE = re.compile(r"/worktrees/|/\.worktrees/|chezmoi-worktrees")
MARKER_RE = re.compile(r"<!-- session: ([0-9a-f-]+) -->")
DEFAULT_PAUSE_SECONDS = 15 * 60
UV = "/opt/homebrew/bin/uv" if Path("/opt/homebrew/bin/uv").exists() else "uv"


def log(msg: str) -> None:
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(BACKFILL_LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


# ── Guards (mirror ~/.local/bin/session-capture-hook) ─────────────────

def in_linked_worktree(cwd: str) -> bool:
    """Same walk as the hook: nearest .git up from cwd is a file."""
    path = Path(cwd) if cwd else None
    while path is not None:
        dot_git = path / ".git"
        if dot_git.is_file():
            return True
        if dot_git.is_dir():
            return False
        if path.parent == path:
            break
        path = path.parent
    return False


def throwaway_cwd(cwd: str) -> str | None:
    """Reason to skip, or None. A deleted worktree cannot be walked, so its
    path shape (Archon workspaces, chezmoi-work, .worktrees/) decides."""
    if not cwd:
        return None
    if Path(cwd).exists():
        return "linked git worktree" if in_linked_worktree(cwd) else None
    if WORKTREE_PATH_RE.search(cwd):
        return "deleted worktree path"
    return None


# ── Transcript discovery ──────────────────────────────────────────────

def transcript_meta(path: Path) -> dict | None:
    """First timestamp (local day + HH:MM), cwd and entrypoint of a transcript."""
    first_ts = cwd = entrypoint = None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if first_ts is None and e.get("timestamp"):
                first_ts = e["timestamp"]
            if cwd is None and e.get("cwd"):
                cwd = e["cwd"]
            if entrypoint is None and e.get("entrypoint"):
                entrypoint = e["entrypoint"]
            if first_ts and cwd and entrypoint:
                break
    if not first_ts:
        return None
    local = datetime.fromisoformat(first_ts.replace("Z", "+00:00")).astimezone(TZ)
    return {
        "path": str(path),
        "session_id": path.stem,
        "date": local.strftime("%Y-%m-%d"),
        "time": local.strftime("%H:%M"),
        "cwd": cwd or "",
        "entrypoint": entrypoint or "",
    }


def legacy_flushed_days() -> set[str]:
    """Days whose log already holds old-hook session entries (no markers).
    Those sessions were flushed when they happened; re-flushing would duplicate."""
    days: set[str] = set()
    for p in DAILY_DIR.glob("*.md"):
        day = p.name[:10]
        text = p.read_text(encoding="utf-8", errors="replace")
        if re.search(r"^### (Session|Memory Flush|Pre-Compaction Flush) \(", text, re.M) and not MARKER_RE.search(text):
            days.add(day)
    return days


def flushed_sessions() -> set[str]:
    ids: set[str] = set()
    for p in DAILY_DIR.glob("*.md"):
        ids.update(MARKER_RE.findall(p.read_text(encoding="utf-8", errors="replace")))
    return ids


def discover(days: set[str] | None, since: str | None, include_today: bool = False) -> tuple[list[dict], dict[str, int]]:
    items: list[dict] = []
    skipped: dict[str, int] = {}
    legacy = legacy_flushed_days()
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    for f in sorted(glob.glob(str(PROJECTS_DIR / "*" / "*.jsonl"))):
        meta = transcript_meta(Path(f))
        if meta is None:
            skipped["no timestamp"] = skipped.get("no timestamp", 0) + 1
            continue
        if days and meta["date"] not in days:
            continue
        if since and meta["date"] < since:
            continue
        reason = throwaway_cwd(meta["cwd"])
        if reason is None and meta["entrypoint"] == "sdk-py" and meta["cwd"].rstrip("/") == str(ROOT):
            reason = "compiler-spawned session"
        if reason is None and meta["date"] in legacy:
            reason = "day already flushed by the old hook"
        if reason is None and not include_today and meta["date"] >= today:
            reason = "today (the live hook flushes it at session end)"
        if reason:
            skipped[reason] = skipped.get(reason, 0) + 1
            meta["skip"] = reason
        items.append(meta)
    items.sort(key=lambda m: (m["date"], m["time"], m["session_id"]))
    return items, skipped


# ── State ─────────────────────────────────────────────────────────────

def load_bstate() -> dict:
    if BACKFILL_STATE.exists():
        try:
            return json.loads(BACKFILL_STATE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"sessions": {}, "compiled": {}, "pauses": []}


def save_bstate(state: dict) -> None:
    tmp = BACKFILL_STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    tmp.replace(BACKFILL_STATE)


def wait_for_window(resets_at: int | None, state: dict, what: str) -> None:
    now = time.time()
    delay = (resets_at - now + 60) if resets_at and resets_at > now else DEFAULT_PAUSE_SECONDS
    delay = max(60, min(delay, 8 * 3600))
    until = datetime.fromtimestamp(now + delay).strftime("%Y-%m-%d %H:%M")
    log(f"PAUSE {what}: usage limit hit, sleeping {delay/60:.0f} min until {until}")
    state.setdefault("pauses", []).append({"at": datetime.now().isoformat(timespec="seconds"), "until": until, "what": what})
    save_bstate(state)
    time.sleep(delay)
    log(f"RESUME {what}")


def parse_resets_at(stdout: str) -> int | None:
    m = re.search(r"resets_at=(\d+)", stdout)
    return int(m.group(1)) if m else None


# ── Phases ────────────────────────────────────────────────────────────

def run_flush_phase(items: list[dict], state: dict, limit: int | None) -> dict[str, int]:
    counts: dict[str, int] = {"flushed": 0, "flush_ok": 0, "already": 0, "empty": 0, "error": 0}
    already = flushed_sessions()
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    done = 0
    for meta in items:
        if meta.get("skip"):
            continue
        sid = meta["session_id"]
        prior = state["sessions"].get(sid, {})
        if sid in already or prior.get("status") in ("flushed", "flush_ok", "empty"):
            counts["already"] += 1
            continue
        if limit is not None and done >= limit:
            break
        done += 1
        try:
            context, turns = extract_conversation_context(Path(meta["path"]))
        except Exception as exc:  # noqa: BLE001
            log(f"ERROR extract {sid} {meta['date']}: {exc}")
            state["sessions"][sid] = {"status": "error", "date": meta["date"], "detail": f"extract: {exc}"}
            save_bstate(state)
            counts["error"] += 1
            continue
        if not context.strip():
            state["sessions"][sid] = {"status": "empty", "date": meta["date"]}
            save_bstate(state)
            counts["empty"] += 1
            log(f"skip {sid} {meta['date']} {meta['time']} empty transcript")
            continue
        ctx_file = STATE_DIR / f"backfill-ctx-{sid}.md"
        ctx_file.write_text(context, encoding="utf-8")
        cmd = [UV, "run", "--directory", str(ROOT), "python", str(SCRIPTS_DIR / "flush.py"),
               str(ctx_file), sid, "--date", meta["date"], "--time", meta["time"]]
        attempts = 0
        while True:
            attempts += 1
            started = time.time()
            res = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=str(ROOT))
            out = (res.stdout or "").strip().splitlines()
            last = out[-1] if out else (res.stderr or "").strip().splitlines()[-1:] or [""]
            last = last if isinstance(last, str) else last[0]
            secs = time.time() - started
            if res.returncode == 0:
                status = "flush_ok" if last.startswith("FLUSH_OK") else "flushed"
                counts[status] += 1
                state["sessions"][sid] = {"status": status, "date": meta["date"], "seconds": round(secs)}
                log(f"ok {sid} {meta['date']} {meta['time']} turns={turns} {last} ({secs:.0f}s)")
                break
            if res.returncode == 3:
                wait_for_window(parse_resets_at(res.stdout or ""), state, f"flush {sid}")
                continue
            if attempts < 3:
                log(f"retry {sid} {meta['date']} exit {res.returncode}: {last[:160]}")
                time.sleep(30)
                continue
            counts["error"] += 1
            state["sessions"][sid] = {"status": "error", "date": meta["date"], "detail": last[:300]}
            ctx_file.unlink(missing_ok=True)
            log(f"ERROR {sid} {meta['date']} exit {res.returncode}: {last[:200]}")
            break
        save_bstate(state)
    return counts


def compile_state_days() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8")).get("ingested", {})
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def run_compile_phase(days: set[str] | None, since: str | None, state: dict) -> dict[str, int]:
    from utils import file_hash

    counts = {"compiled": 0, "already": 0, "error": 0}
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    for log_path in sorted(DAILY_DIR.glob("*.md")):
        day = log_path.name[:10]
        if days and day not in days:
            continue
        if since and day < since:
            continue
        prev = compile_state_days().get(log_path.name, {})
        if prev and prev.get("hash") == file_hash(log_path):
            counts["already"] += 1
            continue
        cmd = [UV, "run", "--directory", str(ROOT), "python", str(SCRIPTS_DIR / "compile.py"), "--file", log_path.name]
        while True:
            started = time.time()
            res = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=str(ROOT))
            tail = [ln for ln in (res.stdout or "").splitlines() if ln.strip()][-6:]
            secs = time.time() - started
            if res.returncode == 3:
                wait_for_window(parse_resets_at(res.stdout or ""), state, f"compile {log_path.name}")
                continue
            if res.returncode != 0 or any(ln.strip().startswith("ERROR:") for ln in tail):
                counts["error"] += 1
                detail = " | ".join(tail)[:400] or (res.stderr or "")[-300:]
                state["compiled"][log_path.name] = {"status": "error", "detail": detail}
                log(f"ERROR compile {log_path.name} exit {res.returncode}: {detail}")
            else:
                counts["compiled"] += 1
                cost_line = next((ln.strip() for ln in tail if ln.strip().startswith("Cost:")), "")
                state["compiled"][log_path.name] = {"status": "compiled", "seconds": round(secs)}
                log(f"compiled {log_path.name} ({secs:.0f}s) {cost_line}")
            save_bstate(state)
            break
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill transcripts into the vault")
    parser.add_argument("--dry-run", action="store_true", help="list what would run, no model calls")
    parser.add_argument("--days", help="comma-separated YYYY-MM-DD list to restrict to")
    parser.add_argument("--since", help="only days on or after YYYY-MM-DD")
    parser.add_argument("--limit", type=int, help="flush at most N transcripts this run")
    parser.add_argument("--phase", choices=["flush", "compile", "all"], default="all")
    parser.add_argument("--include-today", action="store_true", help="also flush sessions dated today (normally left to the live hook)")
    args = parser.parse_args()
    days = set(args.days.split(",")) if args.days else None

    ensure_state_dir()
    lock = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(f"another backfill holds {LOCK_FILE}; exiting", file=sys.stderr)
        return 1

    state = load_bstate()
    items, skipped = discover(days, args.since, args.include_today)
    todo = [m for m in items if not m.get("skip")]
    already = flushed_sessions()
    pending = [m for m in todo if m["session_id"] not in already
               and state["sessions"].get(m["session_id"], {}).get("status") not in ("flushed", "flush_ok", "empty")]
    by_day: dict[str, int] = {}
    for m in pending:
        by_day[m["date"]] = by_day.get(m["date"], 0) + 1
    log(f"START phase={args.phase} dry_run={args.dry_run} vault_daily={DAILY_DIR} host={HOST} "
        f"transcripts={len(items)} eligible={len(todo)} pending={len(pending)} days={len(by_day)} skipped={skipped}")
    if args.dry_run:
        for d in sorted(by_day):
            print(f"  {d}: {by_day[d]} transcript(s)")
        return 0

    if args.phase in ("flush", "all"):
        counts = run_flush_phase(items, state, args.limit)
        log(f"FLUSH DONE {counts}")
    if args.phase in ("compile", "all"):
        counts = run_compile_phase(days, args.since, state)
        log(f"COMPILE DONE {counts}")
    log("END")
    return 0


if __name__ == "__main__":
    sys.exit(main())
