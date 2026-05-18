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

    # Knowledge base index (the core retrieval mechanism)
    if INDEX_FILE.exists():
        index_content = INDEX_FILE.read_text(encoding="utf-8")
        parts.append(f"## Wiki Knowledge Base Index\n\n{index_content}")
    else:
        parts.append("## Wiki Knowledge Base Index\n\n(empty - no articles compiled yet)")

    # Recent daily log
    recent_log = get_recent_log()
    parts.append(f"## Recent Wiki Daily Log\n\n{recent_log}")

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
