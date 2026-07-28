"""
SessionStart hook - injects wiki knowledge base context into every conversation.

Reads the knowledge base index and recent daily log from ~/Vaults/secondbrain/,
then injects them as additional context so Claude always has access to
compiled knowledge from past sessions.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

VAULT_DIR = Path.home() / "Vaults" / "secondbrain"
ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE_DIR = VAULT_DIR / "Memory" / "knowledge"
DAILY_DIR = VAULT_DIR / "Memory" / "daily"
INDEX_FILE = KNOWLEDGE_DIR / "index.md"
AGENTS_FILE = ROOT / "AGENTS.md"

MAX_CONTEXT_CHARS = 20_000
MAX_LOG_LINES = 30


def get_recent_log() -> str:
    """Read the most recent daily log (today or yesterday)."""
    today = datetime.now(timezone.utc).astimezone()

    for offset in range(2):
        date = today - timedelta(days=offset)
        log_path = DAILY_DIR / f"{date.strftime('%Y-%m-%d')}.md"
        if log_path.exists():
            lines = log_path.read_text(encoding="utf-8").splitlines()
            recent = lines[-MAX_LOG_LINES:] if len(lines) > MAX_LOG_LINES else lines
            return "\n".join(recent)

    return "(no recent daily log)"


def build_context() -> str:
    """Assemble the context to inject into the conversation."""
    parts = []

    # Daily log is NOT injected here — the second-brain SessionStart hook reads the
    # same Memory/daily/ files and injects it along with today's date. Two copies of
    # one log helped nobody.

    # Knowledge base: inject a pointer, not the index itself. The index is ~73KB of
    # article titles; injecting it blew the whole 20K budget on a truncated alphabet
    # and starved the daily log above. Claude reads it on demand instead.
    if INDEX_FILE.exists():
        article_count = sum(
            1 for line in INDEX_FILE.read_text(encoding="utf-8").splitlines()
            if line.lstrip().startswith("- [[")
        )
        parts.append(
            "## Wiki Knowledge Base\n\n"
            f"{article_count} compiled articles. Index: `{INDEX_FILE}`\n"
            "Read that file (or grep it) when you need to look something up. "
            "Articles link to each other with [[wikilinks]]."
        )
    else:
        parts.append("## Wiki Knowledge Base\n\n(empty - no articles compiled yet)")

    context = "\n\n---\n\n".join(parts)

    # Truncate if too long
    if len(context) > MAX_CONTEXT_CHARS:
        context = context[:MAX_CONTEXT_CHARS] + "\n\n...(truncated)"

    return context


def main():
    context = build_context()

    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }

    print(json.dumps(output))


if __name__ == "__main__":
    main()
