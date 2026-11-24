"""Tests for the wiki compiler twice rule (donovanwatts/dotfiles#1837)."""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_compile(tmp_path: Path, monkeypatch, twice_rule: str | None = None):
    monkeypatch.setenv("WIKI_VAULT_DIR", str(tmp_path / "vault"))
    monkeypatch.setenv("WIKI_STATE_DIR", str(tmp_path / "state"))
    if twice_rule is None:
        monkeypatch.delenv("WIKI_TWICE_RULE", raising=False)
    else:
        monkeypatch.setenv("WIKI_TWICE_RULE", twice_rule)

    for name in ("config", "utils", "compile"):
        sys.modules.pop(name, None)
    scripts_dir = str(ROOT / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    return importlib.import_module("compile")


def make_vault(compile_mod) -> tuple[Path, Path]:
    compile_mod.DAILY_DIR.mkdir(parents=True, exist_ok=True)
    compile_mod.CONCEPTS_DIR.mkdir(parents=True, exist_ok=True)
    compile_mod.CONNECTIONS_DIR.mkdir(parents=True, exist_ok=True)
    (compile_mod.KNOWLEDGE_DIR / "qa").mkdir(parents=True, exist_ok=True)
    compile_mod.LOG_FILE.write_text("# Build Log\n", encoding="utf-8")

    day1 = compile_mod.DAILY_DIR / "2026-11-22-dmacmini.md"
    day1.write_text(
        "# Daily Log: 2026-11-22\n\nDiscussed idempotent queue design.\n", encoding="utf-8"
    )
    day2 = compile_mod.DAILY_DIR / "2026-11-23-dmacmini.md"
    day2.write_text(
        "# Daily Log: 2026-11-23\n\nRevisited idempotent queue design.\n", encoding="utf-8"
    )
    return day1, day2


def write_concept(path: Path, title: str, sources: list[str], created: str = "2026-11-23") -> None:
    sources_yaml = "\n".join(f'  - "{s}"' for s in sources)
    content = (
        "---\n"
        "type: concept\n"
        f'title: "{title}"\n'
        "tags: [ai-workflows, pattern]\n"
        "sources:\n"
        f"{sources_yaml}\n"
        f"created: {created}\n"
        f"updated: {created}\n"
        "status: draft\n"
        'generated: { by: "claude-memory-compiler/0.2", at: 2026-11-23T18:00:00Z }\n'
        "---\n\n"
        f"# {title}\n\n"
        "Idempotent queues make retries safe without double-processing work.\n\n"
        "## Key Points\n\n"
        "- Retries never duplicate side effects.\n"
    )
    path.write_text(content, encoding="utf-8")


class FakeResult:
    total_cost_usd = 0.0
    usage = {"input_tokens": 10, "output_tokens": 5}
    is_error = False
    result = ""


def test_first_sighting_adds_waiting_list_line_and_no_page(tmp_path, monkeypatch):
    """First sighting creates one line on the waiting list and no concept page."""
    c = load_compile(tmp_path, monkeypatch)
    day1, _ = make_vault(c)

    async def fake_model(prompt, options=None):
        c.add_waiting_list_entry(
            "Idempotent Queue Design",
            "Idempotent queues make retries safe without double-processing work.",
            f"daily/{day1.name}",
            "2026-11-22",
        )
        yield FakeResult()

    asyncio.run(c.compile_daily_log(day1, {}, query_fn=fake_model))

    assert list(c.CONCEPTS_DIR.glob("*.md")) == []
    entries = c.parse_waiting_list(c.WAITING_LIST_FILE.read_text(encoding="utf-8"))
    assert len(entries) == 1
    assert entries[0]["name"] == "Idempotent Queue Design"
    assert entries[0]["source"] == f"daily/{day1.name}"
    assert entries[0]["date"] == "2026-11-22"


def test_post_pass_rejects_single_source_page_and_moves_gist_to_waiting_list(
    tmp_path, monkeypatch, capsys
):
    """Deterministic post-pass rejects a newly created single-source concept page."""
    c = load_compile(tmp_path, monkeypatch)
    day1, _ = make_vault(c)
    rogue_page = c.CONCEPTS_DIR / "idempotent-queue-design.md"

    async def fake_rogue_run(prompt, options=None):
        # Model ignores the prompt and tries to create a single-source concept page.
        write_concept(
            rogue_page,
            "Idempotent Queue Design",
            [f"daily/{day1.name}"],
            created="2026-11-22",
        )
        yield FakeResult()

    asyncio.run(c.compile_daily_log(day1, {}, query_fn=fake_rogue_run))

    # The single-source page does not survive.
    assert not rogue_page.exists()
    # Its gist was moved to the waiting list.
    entries = c.parse_waiting_list(c.WAITING_LIST_FILE.read_text(encoding="utf-8"))
    assert len(entries) == 1
    assert entries[0]["name"] == "Idempotent Queue Design"
    assert "Idempotent queues make retries safe" in entries[0]["gist"]
    assert entries[0]["source"] == f"daily/{day1.name}"
    assert entries[0]["date"] == "2026-11-22"
    # Rejection is logged both to stdout and to log.md.
    out = capsys.readouterr().out
    assert "Twice rule: rejected 1 single-source concept(s)" in out
    assert "concepts/idempotent-queue-design.md" in out
    assert "Twice-rule rejected (moved to waiting list): concepts/idempotent-queue-design.md" in (
        c.LOG_FILE.read_text(encoding="utf-8")
    )


def test_second_sighting_different_log_creates_page_and_clears_waiting_list(tmp_path, monkeypatch):
    """Second sighting from a different daily log keeps the page and clears the waiting line."""
    c = load_compile(tmp_path, monkeypatch)
    day1, day2 = make_vault(c)
    c.add_waiting_list_entry(
        "Idempotent Queue Design",
        "Idempotent queues make retries safe without double-processing work.",
        f"daily/{day1.name}",
        "2026-11-22",
    )
    promoted = c.CONCEPTS_DIR / "idempotent-queue-design.md"

    async def fake_second_sighting(prompt, options=None):
        # Even if the model creates the page with both logs and forgets to edit waiting-list.md,
        # the post-pass keeps the page and clears the waiting-list line.
        write_concept(
            promoted,
            "Idempotent Queue Design",
            [f"daily/{day1.name}", f"daily/{day2.name}"],
            created="2026-11-23",
        )
        yield FakeResult()

    asyncio.run(c.compile_daily_log(day2, {}, query_fn=fake_second_sighting))

    assert promoted.exists()
    sources = c.extract_daily_log_sources(promoted.read_text(encoding="utf-8"))
    assert sources == [f"daily/{day1.name}", f"daily/{day2.name}"]
    entries = c.parse_waiting_list(c.WAITING_LIST_FILE.read_text(encoding="utf-8"))
    assert entries == []


def test_second_sighting_same_log_does_not_count(tmp_path, monkeypatch):
    """Repeating the same daily log twice in sources does not satisfy the twice rule."""
    c = load_compile(tmp_path, monkeypatch)
    day1, _ = make_vault(c)
    c.add_waiting_list_entry(
        "Idempotent Queue Design",
        "Idempotent queues make retries safe without double-processing work.",
        f"daily/{day1.name}",
        "2026-11-22",
    )
    same_log_page = c.CONCEPTS_DIR / "idempotent-queue-design.md"

    async def fake_same_log_run(prompt, options=None):
        # Model cites the same daily log twice (including wikilink/anchor variants).
        write_concept(
            same_log_page,
            "Idempotent Queue Design",
            [f"daily/{day1.name}#session-1", f"[[daily/{day1.stem}]]"],
            created="2026-11-22",
        )
        yield FakeResult()

    asyncio.run(c.compile_daily_log(day1, {}, query_fn=fake_same_log_run))

    # Page is rejected because there is only 1 distinct daily log.
    assert not same_log_page.exists()
    # Waiting list keeps exactly one line, not duplicated.
    entries = c.parse_waiting_list(c.WAITING_LIST_FILE.read_text(encoding="utf-8"))
    assert len(entries) == 1
    assert entries[0]["source"] == f"daily/{day1.name}"


def test_existing_page_is_updated_not_duplicated_and_legacy_single_source_survives(
    tmp_path, monkeypatch
):
    """Pre-existing pages are updated in place, never rejected, and duplicates are blocked."""
    c = load_compile(tmp_path, monkeypatch)
    day1, day2 = make_vault(c)
    existing = c.CONCEPTS_DIR / "idempotent-queue-design.md"
    legacy = c.CONCEPTS_DIR / "legacy-single-source.md"
    write_concept(existing, "Idempotent Queue Design", [f"daily/{day1.name}"], created="2026-11-01")
    write_concept(legacy, "Legacy Single Source", [f"daily/{day1.name}"], created="2026-11-01")
    legacy_before = legacy.read_text(encoding="utf-8")

    dup_attempt = c.CONCEPTS_DIR / "idempotent-queue-design-copy.md"

    async def fake_update_run(prompt, options=None):
        # Update existing page with second log.
        write_concept(
            existing,
            "Idempotent Queue Design",
            [f"daily/{day1.name}", f"daily/{day2.name}"],
            created="2026-11-01",
        )
        # Also simulate an accidental duplicate file with the same title.
        write_concept(
            dup_attempt,
            "Idempotent Queue Design",
            [f"daily/{day1.name}", f"daily/{day2.name}"],
            created="2026-11-23",
        )
        yield FakeResult()

    asyncio.run(c.compile_daily_log(day2, {}, query_fn=fake_update_run))

    # Existing page updated, duplicate rejected, untouched legacy single-source page intact.
    assert existing.exists()
    assert not dup_attempt.exists()
    assert legacy.exists()
    assert legacy.read_text(encoding="utf-8") == legacy_before
    assert len(c.extract_daily_log_sources(existing.read_text(encoding="utf-8"))) == 2


def test_prompt_removes_three_to_seven_quota_and_waiting_list_explains_itself(
    tmp_path, monkeypatch
):
    """Prompt drops '3-7' quota, normalizes zero new pages, and waiting list is self-describing."""
    c = load_compile(tmp_path, monkeypatch)
    day1, _ = make_vault(c)
    wl_path = c.ensure_waiting_list()
    wl_text = wl_path.read_text(encoding="utf-8")

    assert "not a wiki page" in wl_text.lower()
    assert "90 days" in wl_text
    assert "dotfiles#1832" in wl_text
    assert "type: waiting-list" in wl_text

    prompt = c.build_prompt(day1, day1.read_text(encoding="utf-8"), "schema", "index", wl_text)
    assert "3-7" not in prompt
    assert "Zero new concept pages" in prompt
    assert str(c.WAITING_LIST_FILE) in prompt


def test_off_switch_restores_single_source_behavior(tmp_path, monkeypatch):
    """Setting WIKI_TWICE_RULE=0 disables waiting-list enforcement and restores old prompt."""
    c = load_compile(tmp_path, monkeypatch, twice_rule="0")
    day1, _ = make_vault(c)
    single_source_page = c.CONCEPTS_DIR / "one-off-idea.md"

    prompt = c.build_prompt(day1, day1.read_text(encoding="utf-8"), "schema", "index")
    assert "3-7" in prompt
    assert not c.twice_rule_enabled()

    async def fake_single_source_run(prompt, options=None):
        write_concept(single_source_page, "One Off Idea", [f"daily/{day1.name}"])
        yield FakeResult()

    asyncio.run(c.compile_daily_log(day1, {}, query_fn=fake_single_source_run))

    # With twice rule off, a single-source concept page survives and no waiting list is created.
    assert single_source_page.exists()
    assert not c.WAITING_LIST_FILE.exists()


def test_structured_sources_and_non_daily_sources_counted_strictly(tmp_path, monkeypatch):
    """Non-daily sources do not count toward the 2-daily-log threshold; OKF objects do."""
    c = load_compile(tmp_path, monkeypatch)

    mixed_text = (
        "---\n"
        "type: concept\n"
        'title: "Mixed Sources"\n'
        "sources:\n"
        '  - "raw/videos/talk.md"\n'
        '  - "daily/2026-11-23-dmacmini.md"\n'
        "---\n\n# Mixed Sources\n"
    )
    assert c.extract_daily_log_sources(mixed_text) == ["daily/2026-11-23-dmacmini.md"]

    structured_text = (
        "---\n"
        "type: concept\n"
        'title: "Structured Sources"\n'
        "sources:\n"
        "  - id: d1\n"
        "    resource: daily/2026-11-22-dmacmini.md\n"
        '  - { id: d2, resource: "Memory/daily/2026-11-23-dmacmini.md" }\n'
        "---\n\n# Structured Sources\n"
    )
    assert c.extract_daily_log_sources(structured_text) == [
        "daily/2026-11-22-dmacmini.md",
        "daily/2026-11-23-dmacmini.md",
    ]


def write_research_page(
    path: Path, title: str, generated_by: str | None, extra_fm: str = ""
) -> None:
    stamp = (
        f'generated: {{ by: "{generated_by}", at: "2026-11-23T21:10:00Z" }}\n'
        if generated_by
        else ""
    )
    path.write_text(
        "---\n"
        "type: concept\n"
        f'title: "{title}"\n'
        "sources:\n"
        "  - raw/videos/some-talk.md\n"
        f"{extra_fm}"
        f"{stamp}"
        "created: 2026-11-23\n"
        "---\n\n"
        f"# {title}\n\nA page the research processor wrote from a video.\n",
        encoding="utf-8",
    )


def test_post_pass_leaves_a_page_another_tool_wrote_during_the_compile(
    tmp_path, monkeypatch, capsys
):
    """A research page that lands mid-compile is not the compiler's to delete (dotfiles#1861)."""
    c = load_compile(tmp_path, monkeypatch)
    day1, _ = make_vault(c)
    stamped = c.CONCEPTS_DIR / "open-code-review.md"
    with_receipt = c.CONCEPTS_DIR / "hobbling-instructions.md"
    raw_only = c.CONCEPTS_DIR / "unstamped-video-page.md"
    rogue = c.CONCEPTS_DIR / "idempotent-queue-design.md"

    async def model_while_research_writes(prompt, options=None):
        write_research_page(stamped, "Open Code Review", "claude-code/research-processor")
        write_research_page(
            with_receipt,
            "Hobbling Instructions",
            None,
            extra_fm="research_items:\n  - research/researched/2026-11-23-x.md\n",
        )
        write_research_page(raw_only, "Unstamped Video Page", None)
        # The compiler's own single-source page in the same run is still rejected.
        write_concept(rogue, "Idempotent Queue Design", [f"daily/{day1.name}"])
        yield FakeResult()

    asyncio.run(c.compile_daily_log(day1, {}, query_fn=model_while_research_writes))

    assert stamped.exists()
    assert with_receipt.exists()
    assert raw_only.exists()
    assert not rogue.exists()
    names = [
        e["name"] for e in c.parse_waiting_list(c.WAITING_LIST_FILE.read_text(encoding="utf-8"))
    ]
    assert names == ["Idempotent Queue Design"]
    out = capsys.readouterr().out
    assert "left 3 page(s) from another writer alone" in out
    assert "concepts/open-code-review.md (generated by claude-code/research-processor)" in out
    assert "Twice rule: rejected 1 single-source concept(s)" in out


def test_compiler_page_duplicating_a_foreign_page_is_rejected_not_the_foreign_one(
    tmp_path, monkeypatch
):
    """A foreign page's name counts as existing, whichever file sorts first."""
    c = load_compile(tmp_path, monkeypatch)
    day1, day2 = make_vault(c)
    foreign = c.CONCEPTS_DIR / "zz-idempotent-queue-design.md"
    dupe = c.CONCEPTS_DIR / "idempotent-queue-design.md"

    async def fake_model(prompt, options=None):
        write_research_page(foreign, "Idempotent Queue Design", "claude-code/research-processor")
        write_concept(dupe, "Idempotent Queue Design", [f"daily/{day1.name}", f"daily/{day2.name}"])
        yield FakeResult()

    asyncio.run(c.compile_daily_log(day2, {}, query_fn=fake_model))

    assert foreign.exists()
    assert not dupe.exists()


def test_written_by_another_tool_reads_the_stamp_in_both_forms(tmp_path, monkeypatch):
    c = load_compile(tmp_path, monkeypatch)
    block = "---\ntitle: X\ngenerated:\n  by: codex/gpt\n  at: 2026-11-23\n---\n\n# X\n"
    assert c.written_by_another_tool(block) == "generated by codex/gpt"
    own = (
        "---\ntitle: X\nsources:\n  - daily/2026-11-23-dmacmini.md\n"
        'generated: { by: "claude-memory-compiler/0.2", at: 2026-11-23T18:00:00Z }\n---\n\n# X\n'
    )
    assert c.written_by_another_tool(own) is None
    # No stamp and no sources: still the compiler's, so the rule still applies to it.
    assert c.written_by_another_tool("---\ntitle: X\n---\n\n# X\n") is None
    # One daily log plus a raw source, unstamped: the compiler's, counted strictly.
    mixed = "---\ntitle: X\nsources:\n  - raw/videos/t.md\n  - daily/2026-11-23-dmacmini.md\n---\n\n# X\n"
    assert c.written_by_another_tool(mixed) is None
