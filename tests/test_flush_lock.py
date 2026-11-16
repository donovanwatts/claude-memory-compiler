"""Two flush.py processes never hold the flush lock at once (dotfiles#1437)."""

import fcntl
import importlib
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_flush(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_VAULT_DIR", str(tmp_path / "vault"))
    monkeypatch.setenv("WIKI_STATE_DIR", str(tmp_path / "state"))
    for name in ("config", "flush"):
        sys.modules.pop(name, None)
    sys.path.insert(0, str(ROOT / "scripts"))
    return importlib.import_module("flush")


def test_second_holder_waits_until_first_releases(tmp_path, monkeypatch):
    flush = load_flush(tmp_path, monkeypatch)
    first = flush.acquire_flush_lock()
    # A second process asking for the same lock must not get it while we hold it.
    probe = (
        "import fcntl, sys\n"
        f"h = open({str(flush.LOCK_FILE)!r}, 'w')\n"
        "try:\n"
        "    fcntl.flock(h, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "except BlockingIOError:\n"
        "    sys.exit(7)\n"
    )
    held = subprocess.run([sys.executable, "-c", probe])
    assert held.returncode == 7
    fcntl.flock(first, fcntl.LOCK_UN)
    first.close()
    time.sleep(0.05)
    free = subprocess.run([sys.executable, "-c", probe])
    assert free.returncode == 0
