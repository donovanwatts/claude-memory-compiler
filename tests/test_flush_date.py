"""flush.py --date files an entry on the given day, with a session marker, and never auto-compiles."""

import importlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_flush(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_VAULT_DIR", str(tmp_path / "vault"))
    monkeypatch.setenv("WIKI_STATE_DIR", str(tmp_path / "state"))
    for name in ("config", "flush"):
        sys.modules.pop(name, None)
    sys.path.insert(0, str(ROOT / "scripts"))
    return importlib.import_module("flush")


def test_append_uses_given_date_time_and_marker(tmp_path, monkeypatch):
    flush = load_flush(tmp_path, monkeypatch)
    path = flush.append_to_daily_log("hello", "Session", "abc-123", date="2026-10-05", time_str="09:41")
    assert path.name.startswith("2026-10-05-")
    text = path.read_text()
    assert "title: 2026-10-05" in text
    assert "### Session (09:41)\n\n<!-- session: abc-123 -->\nhello\n" in text


def test_append_defaults_to_today(tmp_path, monkeypatch):
    flush = load_flush(tmp_path, monkeypatch)
    path = flush.append_to_daily_log("x", "Session", "s1")
    assert path.name.startswith(flush.datetime.now().strftime("%Y-%m-%d"))


def test_parse_args_validates_date():
    sys.path.insert(0, str(ROOT / "scripts"))
    flush = importlib.import_module("flush")
    args = flush.parse_args(["ctx.md", "sid", "--date", "2026-10-05", "--time", "09:41"])
    assert (args.date, args.time) == ("2026-10-05", "09:41")
    try:
        flush.parse_args(["ctx.md", "sid", "--date", "2026-13-05"])
    except ValueError:
        pass
    else:
        raise AssertionError("bad date accepted")


def test_backfill_dry_run_lists_without_model(tmp_path):
    """A fake transcript on a known day is discovered, dated locally, and a worktree one is skipped."""
    projects = tmp_path / "projects"
    (projects / "p-main").mkdir(parents=True)
    (projects / "p-wt").mkdir(parents=True)
    wt = tmp_path / "repo" / ".worktrees" / "x"
    wt.mkdir(parents=True)
    (wt / ".git").write_text("gitdir: elsewhere")
    line = '{"type":"user","timestamp":"2026-10-05T15:41:00Z","cwd":"%s","entrypoint":"cli","message":{"role":"user","content":"hi"}}\n'
    (projects / "p-main" / "aaaa-1111.jsonl").write_text(line % str(tmp_path))
    (projects / "p-wt" / "bbbb-2222.jsonl").write_text(line % str(wt))
    env = {**os.environ, "WIKI_VAULT_DIR": str(tmp_path / "vault"), "WIKI_STATE_DIR": str(tmp_path / "state"),
           "CLAUDE_PROJECTS_DIR": str(projects)}
    (tmp_path / "vault" / "Memory" / "daily").mkdir(parents=True)
    res = subprocess.run([sys.executable, str(ROOT / "scripts" / "backfill.py"), "--dry-run"],
                         capture_output=True, text=True, env=env, cwd=str(ROOT / "scripts"))
    assert res.returncode == 0, res.stderr
    assert "2026-10-05: 1 transcript(s)" in res.stdout
    assert "'linked git worktree': 1" in res.stdout
