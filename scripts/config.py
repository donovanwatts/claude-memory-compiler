"""Path constants and configuration for the personal knowledge base.

Two environment overrides exist so a run can be pointed at a scratch copy
instead of the live vault (used by tests and by the acceptance run on
secondbrain#22):

    WIKI_VAULT_DIR   root of the vault (default ~/Vaults/secondbrain)
    WIKI_STATE_DIR   where state.json, last-flush.json and the logs live
                     (default ~/.local/state/wiki-compiler, XDG state dir)
"""

import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────
ROOT_DIR = Path(__file__).resolve().parent.parent
VAULT_DIR = Path(os.environ.get("WIKI_VAULT_DIR") or (Path.home() / "Vaults" / "secondbrain")).expanduser()
DAILY_DIR = VAULT_DIR / "Memory" / "daily"
KNOWLEDGE_DIR = VAULT_DIR / "Memory" / "knowledge"
CONCEPTS_DIR = KNOWLEDGE_DIR / "concepts"
CONNECTIONS_DIR = KNOWLEDGE_DIR / "connections"
QA_DIR = KNOWLEDGE_DIR / "qa"
REPORTS_DIR = VAULT_DIR / "Memory" / "reports"
SCRIPTS_DIR = ROOT_DIR / "scripts"
HOOKS_DIR = ROOT_DIR / "hooks"
AGENTS_FILE = ROOT_DIR / "AGENTS.md"

# Vault-side OKF tools. The compiler defers index generation to okf_reindex.py
# when it exists, so there is exactly one writer of index.md.
OKF_TOOLS_DIR = KNOWLEDGE_DIR / "_tools"
OKF_REINDEX = OKF_TOOLS_DIR / "okf_reindex.py"
OKF_VALIDATE = OKF_TOOLS_DIR / "okf_validate.py"

INDEX_FILE = KNOWLEDGE_DIR / "index.md"
LOG_FILE = KNOWLEDGE_DIR / "log.md"

_xdg_state = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))
STATE_DIR = Path(os.environ.get("WIKI_STATE_DIR") or (_xdg_state / "wiki-compiler")).expanduser()
STATE_FILE = STATE_DIR / "state.json"
FLUSH_STATE_FILE = STATE_DIR / "last-flush.json"
FLUSH_LOG = STATE_DIR / "flush.log"
COMPILE_LOG = STATE_DIR / "compile.log"
HOOK_LOG = STATE_DIR / "hook.log"


def ensure_state_dir() -> Path:
    """Create the state dir. Migrate the pre-XDG scripts/state.json once."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    legacy = SCRIPTS_DIR / "state.json"
    if not STATE_FILE.exists() and legacy.exists() and "WIKI_STATE_DIR" not in os.environ:
        shutil.copy2(legacy, STATE_FILE)
    return STATE_DIR


# ── Model routing ──────────────────────────────────────────────────────
# Every model call goes through the Claude Agent SDK, which runs the Claude
# Code CLI and authenticates with the interactive Claude Max login (Keychain
# on macOS). No Anthropic API key is read or set anywhere in this repo.
# Sonnet by default; nothing here is money-critical.
COMPILE_MODEL = os.environ.get("WIKI_COMPILE_MODEL", "claude-sonnet-5-5")
FLUSH_MODEL = os.environ.get("WIKI_FLUSH_MODEL", "claude-sonnet-5-5")
COMPILER_ACTOR = "claude-memory-compiler/0.2"

# ── Timezone ───────────────────────────────────────────────────────────
TIMEZONE = "America/Denver"


def now_iso() -> str:
    """Current time in ISO 8601 format."""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def now_utc_iso() -> str:
    """Current UTC time, OKF `generated.at` style: 2026-10-31T17:00:00Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_iso() -> str:
    """Current date in ISO 8601 format."""
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")


def lean_sdk_options(**overrides):
    """ClaudeAgentOptions that load nothing from the user's Claude Code setup.

    Measured 2026-10-31: a one-word call with default options pulled 540k
    tokens of MCP tool definitions, skills and CLAUDE.md into context. With
    these options the same call is about 50k. Auth is unchanged: the CLI still
    uses the Claude Max login.
    """
    from claude_agent_sdk import ClaudeAgentOptions

    extra_args = {
        "strict-mcp-config": None,
        "disable-slash-commands": None,
        "no-session-persistence": None,
    }
    extra_args.update(overrides.pop("extra_args", {}))
    base = dict(
        setting_sources=[],
        mcp_servers={},
        extra_args=extra_args,
        env={"CLAUDE_INVOKED_BY": "memory_flush"},
    )
    base.update(overrides)
    return ClaudeAgentOptions(**base)
