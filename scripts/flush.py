"""
Memory flush agent - extracts important knowledge from conversation context.

Spawned by session-end.py or pre-compact.py as a background process. Reads
pre-extracted conversation context from a .md file, uses the Claude Agent SDK
to decide what's worth saving, and appends the result to today's daily log.

Usage:
    uv run python flush.py <context_file.md> <session_id> [--date YYYY-MM-DD] [--time HH:MM]

`--date` files the entry under that day's log instead of today's (used by
scripts/backfill.py so a transcript lands on its own day). `--time` is the
entry's clock label and defaults to now. Every entry starts with a
`<!-- session: <id> -->` marker so a re-run can tell it has already been flushed.

Exit codes: 0 written, 2 model error (nothing written to the vault),
3 usage limit hit (nothing written; stdout carries `resets_at=<unix>` when known).
"""

from __future__ import annotations

# Recursion prevention: set this BEFORE any imports that might trigger Claude
import os
os.environ["CLAUDE_INVOKED_BY"] = "memory_flush"

import argparse
import asyncio
import json
import logging
import re
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from config import (
    COMPILE_LOG,
    DAILY_DIR,
    FLUSH_LOG,
    FLUSH_MODEL,
    FLUSH_STATE_FILE,
    STATE_FILE as COMPILE_STATE_FILE,
    ensure_state_dir,
    lean_sdk_options,
)

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
STATE_FILE = FLUSH_STATE_FILE
LOG_FILE = FLUSH_LOG
ensure_state_dir()

# Set up file-based logging so we can verify the background process ran.
# The parent process sends stdout/stderr to DEVNULL (to avoid the inherited
# file handle bug on Windows), so this is our only observability channel.
logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def load_flush_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_flush_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state), encoding="utf-8")


def session_marker(session_id: str) -> str:
    return f"<!-- session: {session_id} -->"


def append_to_daily_log(
    content: str,
    section: str = "Session",
    session_id: str = "",
    date: str | None = None,
    time_str: str | None = None,
) -> Path:
    """Append content to the daily log for `date` (default today). Returns the path."""
    today = datetime.now(timezone.utc).astimezone()
    date = date or today.strftime("%Y-%m-%d")
    # One file per day per machine (vault convention since 2026-08: the host
    # suffix keeps two machines from editing the same file into a git conflict).
    host = socket.gethostname().split(".")[0].lower() or "unknown-host"
    log_path = DAILY_DIR / f"{date}-{host}.md"

    if not log_path.exists():
        DAILY_DIR.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            f"---\ntitle: {date}\ntype: daily\n---\n# {date} ({host})\n\n## Sessions\n\n",
            encoding="utf-8",
        )

    time_str = time_str or today.strftime("%H:%M")
    marker = f"{session_marker(session_id)}\n" if session_id else ""
    entry = f"### {section} ({time_str})\n\n{marker}{content}\n\n"

    with open(log_path, "a", encoding="utf-8") as f:
        f.write(entry)
    return log_path


LIMIT_TEXT_RE = re.compile(r"hit your limit|usage limit|rate limit", re.I)


async def run_flush(context: str) -> str:
    """Use Claude Agent SDK to extract important knowledge from conversation context."""
    from claude_agent_sdk import AssistantMessage, RateLimitEvent, ResultMessage, TextBlock, query

    prompt = f"""Review the conversation context below and respond with a concise summary
of important items that should be preserved in the daily log.
Do NOT use any tools — just return plain text.

Format your response as a structured daily log entry with these sections:

**Context:** [One line about what the user was working on]

**Key Exchanges:**
- [Important Q&A or discussions]

**Decisions Made:**
- [Any decisions with rationale]

**Lessons Learned:**
- [Gotchas, patterns, or insights discovered]

**Action Items:**
- [Follow-ups or TODOs mentioned]

Skip anything that is:
- Routine tool calls or file reads
- Content that's trivial or obvious
- Trivial back-and-forth or clarification exchanges

Only include sections that have actual content. If nothing is worth saving,
respond with exactly: FLUSH_OK

## Conversation Context

{context}"""

    response = ""
    limit_hit = False
    resets_at = None

    try:
        async for message in query(
            prompt=prompt,
            options=lean_sdk_options(
                cwd=str(ROOT),
                system_prompt="You are a terse note-taker. No tools. Plain text only.",
                allowed_tools=[],
                extra_args={"tools": ""},
                max_turns=2,
                model=FLUSH_MODEL,
            ),
        ):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        response += block.text
            elif isinstance(message, RateLimitEvent):
                info = message.rate_limit_info
                logging.info(
                    "Rate limit %s type=%s utilization=%s resets_at=%s",
                    info.status, info.rate_limit_type, info.utilization, info.resets_at,
                )
                if info.status == "rejected":
                    limit_hit = True
                    resets_at = info.resets_at
            elif isinstance(message, ResultMessage):
                u = message.usage or {}
                logging.info(
                    "Model %s cost=$%.4f (notional) ms=%s in=%s cache_create=%s cache_read=%s out=%s",
                    FLUSH_MODEL, message.total_cost_usd or 0.0, message.duration_ms,
                    u.get("input_tokens"), u.get("cache_creation_input_tokens"),
                    u.get("cache_read_input_tokens"), u.get("output_tokens"),
                )
                if message.is_error:
                    text = (message.result or "")[:200]
                    if LIMIT_TEXT_RE.search(text):
                        limit_hit = True
                    response = f"FLUSH_ERROR: result subtype={message.subtype} {text}".strip()
    except Exception as e:
        import traceback
        logging.error("Agent SDK error: %s\n%s", e, traceback.format_exc())
        response = f"FLUSH_ERROR: {type(e).__name__}: {e}"

    if limit_hit:
        response = f"FLUSH_PAUSED resets_at={resets_at or ''}"
    return response



COMPILE_AFTER_HOUR = 18  # 6 PM local time


def maybe_trigger_compilation() -> None:
    """If it's past the compile hour and today's log hasn't been compiled, run compile.py."""
    import subprocess as _sp

    now = datetime.now(timezone.utc).astimezone()
    if now.hour < COMPILE_AFTER_HOUR:
        return

    # Check if today's log has already been compiled
    host = socket.gethostname().split(".")[0].lower() or "unknown-host"
    today_log = f"{now.strftime('%Y-%m-%d')}-{host}.md"
    compile_state_file = COMPILE_STATE_FILE
    if compile_state_file.exists():
        try:
            compile_state = json.loads(compile_state_file.read_text(encoding="utf-8"))
            ingested = compile_state.get("ingested", {})
            if today_log in ingested:
                # Already compiled today - check if the log has changed since
                from hashlib import sha256
                log_path = DAILY_DIR / today_log
                if log_path.exists():
                    current_hash = sha256(log_path.read_bytes()).hexdigest()[:16]
                    if ingested[today_log].get("hash") == current_hash:
                        return  # log unchanged since last compile
        except (json.JSONDecodeError, OSError):
            pass

    compile_script = SCRIPTS_DIR / "compile.py"
    if not compile_script.exists():
        return

    logging.info("End-of-day compilation triggered (after %d:00)", COMPILE_AFTER_HOUR)

    cmd = ["uv", "run", "--directory", str(ROOT), "python", str(compile_script)]

    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = _sp.CREATE_NEW_PROCESS_GROUP | _sp.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True

    try:
        log_handle = open(str(COMPILE_LOG), "a")
        log_handle.write(f"\n=== {now.isoformat(timespec='seconds')} compile spawned by flush ===\n")
        log_handle.flush()
        _sp.Popen(cmd, stdout=log_handle, stderr=_sp.STDOUT, cwd=str(ROOT), **kwargs)
    except Exception as e:
        logging.error("Failed to spawn compile.py: %s", e)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Flush one session's context into the daily log")
    parser.add_argument("context_file", type=Path)
    parser.add_argument("session_id")
    parser.add_argument("--date", help="YYYY-MM-DD to file the entry under (default: today)")
    parser.add_argument("--time", help="HH:MM label for the entry (default: now)")
    args = parser.parse_args(argv)
    if args.date:
        datetime.strptime(args.date, "%Y-%m-%d")
    if args.time:
        datetime.strptime(args.time, "%H:%M")
    return args


def main():
    args = parse_args()
    context_file: Path = args.context_file
    session_id: str = args.session_id

    logging.info("flush.py started for session %s, context: %s", session_id, context_file)

    if not context_file.exists():
        logging.error("Context file not found: %s", context_file)
        return

    # Deduplication: skip if same session was flushed within 60 seconds
    state = load_flush_state()
    if (
        state.get("session_id") == session_id
        and time.time() - state.get("timestamp", 0) < 60
    ):
        logging.info("Skipping duplicate flush for session %s", session_id)
        context_file.unlink(missing_ok=True)
        return

    # Read pre-extracted context
    context = context_file.read_text(encoding="utf-8").strip()
    if not context:
        logging.info("Context file is empty, skipping")
        context_file.unlink(missing_ok=True)
        return

    logging.info("Flushing session %s: %d chars", session_id, len(context))

    # Run the LLM extraction
    response = asyncio.run(run_flush(context))

    # A failed call writes nothing to the vault: the context file is kept so the
    # caller (hook or backfill) can retry, and the exit code says why.
    if response.startswith("FLUSH_PAUSED"):
        logging.warning("Result: %s (usage limit; nothing written)", response)
        print(response)
        sys.exit(3)
    if "FLUSH_ERROR" in response:
        logging.error("Result: %s (nothing written)", response)
        print(response)
        sys.exit(2)

    if "FLUSH_OK" in response:
        logging.info("Result: FLUSH_OK")
        append_to_daily_log(
            "FLUSH_OK - Nothing worth saving from this session", "Memory Flush",
            session_id, args.date, args.time,
        )
        print("FLUSH_OK")
    else:
        logging.info("Result: saved to daily log (%d chars)", len(response))
        append_to_daily_log(response, "Session", session_id, args.date, args.time)
        print(f"SAVED {len(response)} chars")

    # Update dedup state
    save_flush_state({"session_id": session_id, "timestamp": time.time()})

    # Clean up context file
    context_file.unlink(missing_ok=True)

    # End-of-day auto-compilation: if it's past the compile hour and today's
    # log hasn't been compiled yet, trigger compile.py in the background.
    # A backfill (--date) compiles separately, so it never triggers this.
    if not args.date:
        maybe_trigger_compilation()

    logging.info("Flush complete for session %s", session_id)


if __name__ == "__main__":
    main()
