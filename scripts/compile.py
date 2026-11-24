"""
Compile daily conversation logs into structured knowledge articles.

This is the "LLM compiler" - it reads daily logs (source code) and produces
organized knowledge articles (the executable).

Usage (exit 3 means the usage window is exhausted; re-run after it resets):
    uv run python compile.py                    # compile new/changed logs only
    uv run python compile.py --all              # force recompile everything
    uv run python compile.py --file daily/2026-04-01.md  # compile a specific log
    uv run python compile.py --dry-run          # show what would be compiled

Output contract (secondbrain#22, dotfiles#1837): every page the compiler writes
carries a non-empty `type` and an OKF v0.2 `generated` field; log.md headings
are plain `## YYYY-MM-DD`; index.md is never written by the model - the vault's
okf_reindex.py regenerates it after each run, so there is one index writer.
Under the twice rule (on by default; set WIKI_TWICE_RULE=0 to disable), a new
concept page is created only on a second sighting from a distinct daily log;
first sightings are recorded as one line in `waiting-list.md`.
"""

from __future__ import annotations

# Recursion guard: the SDK runs the Claude Code CLI, which fires the user's
# SessionEnd/PreCompact hooks, which would spawn another flush. Set before
# any import that could reach Claude.
import os

os.environ["CLAUDE_INVOKED_BY"] = "memory_flush"

import argparse
import asyncio
import re
import subprocess
import sys
import time
from pathlib import Path

from config import (
    AGENTS_FILE,
    COMPILE_MODEL,
    COMPILER_ACTOR,
    CONCEPTS_DIR,
    CONNECTIONS_DIR,
    DAILY_DIR,
    KNOWLEDGE_DIR,
    LOG_FILE,
    OKF_REINDEX,
    VAULT_DIR,
    WAITING_LIST_FILE,
    lean_sdk_options,
    now_iso,
    now_utc_iso,
    today_iso,
    twice_rule_enabled,
)
from utils import (
    file_hash,
    list_raw_files,
    list_wiki_articles,
    load_state,
    read_wiki_index,
    save_state,
    slugify,
)

ROOT_DIR = Path(__file__).resolve().parent.parent

FM_RE = re.compile(r"^---\n(.*?)\n---\n?", re.S)
TYPE_RE = re.compile(r"^type:\s*\S+", re.M)
GENERATED_RE = re.compile(r"^generated:", re.M)
BRACKET_HEADING_RE = re.compile(r"^##\s+\[(\d{4}-\d{2}-\d{2})[^\]]*\]\s*$", re.M)
LIMIT_TEXT_RE = re.compile(r"hit your limit|usage limit|rate limit", re.I)
ISO_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
# `generated.by` stamps this compiler writes start with this; anything else is another writer.
COMPILER_WRITER_PREFIX = "claude-memory-compiler"
BARE_DAILY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:-[A-Za-z0-9_.-]+)?(?:\.md)?$")
TITLE_H1_RE = re.compile(r"^#\s+(.+)$", re.M)


def _waiting_list_template(today: str, generated_at: str) -> str:
    return (
        "---\n"
        "type: waiting-list\n"
        'title: "Concept Waiting List"\n'
        "status: stable\n"
        f"created: {today}\n"
        f"updated: {today}\n"
        "tags:\n"
        "  - llm-wiki\n"
        "  - knowledge\n"
        f'generated: {{ by: "{COMPILER_ACTOR}", at: {generated_at} }}\n'
        "---\n\n"
        "# Concept Waiting List\n\n"
        "This file holds first sightings of candidate wiki concepts found during daily log "
        "compilation. A line here is not a wiki page: under the twice rule, an idea gets its "
        "own page in `concepts/` only when it shows up in a second, distinct daily log.\n\n"
        "Lines older than 90 days are listed by the weekly prune report (dotfiles#1832), "
        "not deleted here.\n\n"
        "## Candidates\n\n"
        "<!-- Format: - **Short Name** | One-sentence gist. | "
        "source: daily/YYYY-MM-DD-host.md | date: YYYY-MM-DD -->\n"
    )


def ensure_waiting_list(path: Path | None = None) -> Path:
    """Create the waiting list file with OKF frontmatter if it does not exist."""
    target = path or WAITING_LIST_FILE
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_waiting_list_template(today_iso(), now_utc_iso()), encoding="utf-8")
    return target


def read_waiting_list(path: Path | None = None) -> str:
    """Read the waiting list file, or return a placeholder if it does not exist yet."""
    target = path or WAITING_LIST_FILE
    if target.exists():
        return target.read_text(encoding="utf-8")
    return _waiting_list_template(today_iso(), now_utc_iso())


def canonical_daily_source(raw: str) -> str | None:
    """Normalize a daily-log reference to `daily/<filename>.md`, or None if not a daily log."""
    s = raw.strip().strip("\"'").rstrip(",").strip().strip("\"'")
    if not s:
        return None
    if s.startswith("[[") and s.endswith("]]"):
        s = s[2:-2].strip()
    if "|" in s:
        s = s.split("|", 1)[0].strip()
    if "#" in s:
        s = s.split("#", 1)[0].strip()
    s = s.lstrip("./")
    if s.startswith("Memory/daily/"):
        s = s[len("Memory/") :]
    if s.startswith("daily/"):
        name = s[len("daily/") :]
    elif BARE_DAILY_RE.match(s):
        name = s
    else:
        return None
    if not name or "/" in name:
        return None
    if not name.endswith(".md"):
        name += ".md"
    return f"daily/{name}"


def _split_frontmatter(text: str) -> tuple[str | None, str]:
    m = FM_RE.match(text)
    if not m:
        return None, text
    return m.group(1), text[m.end() :]


def _frontmatter_field(fm: str, key: str) -> str:
    m = re.search(rf"^{re.escape(key)}:\s*(.+)$", fm, re.M)
    if not m:
        return ""
    return m.group(1).strip().strip("\"'")


def _frontmatter_list(fm: str, key: str) -> list[str]:
    lines = fm.splitlines()
    for i, line in enumerate(lines):
        m = re.match(rf"^{re.escape(key)}:\s*(.*)$", line)
        if not m:
            continue
        rest = m.group(1).strip()
        if rest:
            if rest.startswith("[") and rest.endswith("]"):
                inner = rest[1:-1].strip()
                if not inner:
                    return []
                return [item.strip().strip("\"'") for item in inner.split(",") if item.strip()]
            return [rest.strip("\"'")]
        items: list[str] = []
        for follow in lines[i + 1 :]:
            if follow and not follow.startswith((" ", "\t", "-")):
                break
            stripped = follow.strip()
            if stripped.startswith("- "):
                items.append(stripped[2:].strip().strip("\"'"))
        return items
    return []


def extract_daily_log_sources(text: str) -> list[str]:
    """Return deduplicated canonical `daily/<file>.md` sources from YAML frontmatter."""
    fm, _ = _split_frontmatter(text)
    if not fm:
        return []
    lines = fm.splitlines()
    candidates: list[str] = []
    for i, line in enumerate(lines):
        m = re.match(r"^sources:\s*(.*)$", line)
        if not m:
            continue
        rest = m.group(1).strip()
        if rest:
            # Handle inline list of strings or inline objects with resource: ...
            if "resource:" in rest:
                for res_match in re.finditer(r'resource:\s*["\']?([^"\',}\]]+)', rest):
                    candidates.append(res_match.group(1))
            elif rest.startswith("[") and rest.endswith("]"):
                inner = rest[1:-1].strip()
                if inner:
                    candidates.extend(part.strip() for part in inner.split(","))
            else:
                candidates.append(rest)
            break

        for follow in lines[i + 1 :]:
            if follow and not follow.startswith((" ", "\t", "-")):
                break
            stripped = follow.strip()
            if not stripped:
                continue
            if "resource:" in stripped:
                res_match = re.search(r'resource:\s*["\']?([^"\',}\]]+)', stripped)
                if res_match:
                    candidates.append(res_match.group(1))
            elif stripped.startswith("- "):
                val = stripped[2:].strip()
                # Skip structured mapping keys like `- id: ...` when resource is on a later line.
                if re.match(r"^[A-Za-z0-9_-]+:\s+", val):
                    continue
                candidates.append(val)
        break

    seen: set[str] = set()
    result: list[str] = []
    for raw in candidates:
        canon = canonical_daily_source(raw)
        if canon and canon not in seen:
            seen.add(canon)
            result.append(canon)
    return result


def _first_sentence(body: str) -> str:
    in_code = False
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if in_code or not stripped or stripped.startswith(("#", "|", "---", "<!--", "![")):
            continue
        stripped = re.sub(r"^[-*]\s+", "", stripped)
        stripped = re.sub(r"^\d+\.\s+", "", stripped)
        stripped = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", stripped)
        stripped = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2", stripped)
        stripped = stripped.replace("[[", "").replace("]]", "")
        stripped = stripped.replace("|", "-")
        stripped = re.sub(r"\s+", " ", stripped).strip()
        if not stripped:
            continue
        sent_match = re.match(r"^(.+?[.!?])(?:\s|$)", stripped)
        sentence = sent_match.group(1) if sent_match else stripped
        return sentence[:180].strip()
    return ""


def _extract_article_meta(path: Path, text: str) -> dict:
    fm, body = _split_frontmatter(text)
    title = _frontmatter_field(fm, "title") if fm else ""
    if not title:
        h1 = TITLE_H1_RE.search(body)
        title = h1.group(1).strip() if h1 else path.stem.replace("-", " ").title()
    created = _frontmatter_field(fm, "created") if fm else ""
    aliases = _frontmatter_list(fm, "aliases") if fm else []
    gist = _first_sentence(body) or title

    match_keys = {k for k in (slugify(path.stem), slugify(title)) if k}
    for alias in aliases:
        s = slugify(alias)
        if s:
            match_keys.add(s)

    return {
        "title": title,
        "gist": gist,
        "created": created,
        "match_keys": match_keys,
    }


def _parse_waiting_line(line: str) -> dict[str, str] | None:
    stripped = line.strip()
    if not stripped.startswith("- ") or "|" not in stripped:
        return None
    parts = [p.strip() for p in stripped[2:].split("|")]
    if len(parts) < 4:
        return None
    raw_name = parts[0].strip("*").strip()
    gist = parts[1].strip()
    raw_source = re.sub(r"^source:\s*", "", parts[2], flags=re.I).strip()
    raw_date = re.sub(r"^date:\s*", "", parts[3], flags=re.I).strip()
    if not raw_name:
        return None
    canon_source = canonical_daily_source(raw_source) or raw_source
    return {
        "name": raw_name,
        "slug": slugify(raw_name),
        "gist": gist,
        "source": canon_source,
        "date": raw_date,
        "raw_line": line,
    }


def parse_waiting_list(text: str) -> list[dict[str, str]]:
    """Parse candidate entries from waiting-list.md content."""
    entries: list[dict[str, str]] = []
    for line in text.splitlines():
        parsed = _parse_waiting_line(line)
        if parsed:
            entries.append(parsed)
    return entries


def format_waiting_list_line(name: str, gist: str, source: str, date: str) -> str:
    """Format one single-line waiting-list entry."""
    clean_name = re.sub(r"\s+", " ", name.replace("|", "-").replace("*", "")).strip()
    clean_gist = re.sub(r"\s+", " ", gist.replace("|", "-")).strip()
    canon = canonical_daily_source(source) or source.strip()
    return f"- **{clean_name}** | {clean_gist} | source: {canon} | date: {date.strip()}"


def _bump_updated_date(text: str, date: str) -> str:
    return re.sub(r"^updated:\s*.*$", f"updated: {date}", text, count=1, flags=re.M)


def add_waiting_list_entry(
    name: str,
    gist: str,
    source: str,
    date: str,
    path: Path | None = None,
) -> bool:
    """Append a first-sighting line unless an entry for the same concept already exists."""
    target = ensure_waiting_list(path)
    text = target.read_text(encoding="utf-8")
    key = slugify(name)
    for existing in parse_waiting_list(text):
        if existing["slug"] == key:
            return False

    line = format_waiting_list_line(name, gist, source, date)
    if not text.endswith("\n"):
        text += "\n"
    text = _bump_updated_date(text, today_iso()) + line + "\n"
    target.write_text(text, encoding="utf-8")
    return True


def remove_waiting_list_entries(match_keys: set[str], path: Path | None = None) -> list[str]:
    """Remove any waiting-list line matching one of `match_keys` (slugified names)."""
    target = path or WAITING_LIST_FILE
    if not target.exists() or not match_keys:
        return []
    text = target.read_text(encoding="utf-8")
    kept: list[str] = []
    removed: list[str] = []
    for line in text.splitlines():
        parsed = _parse_waiting_line(line)
        if parsed and parsed["slug"] in match_keys:
            removed.append(parsed["name"])
        else:
            kept.append(line)
    if not removed:
        return []
    new_text = _bump_updated_date("\n".join(kept).rstrip("\n") + "\n", today_iso())
    target.write_text(new_text, encoding="utf-8")
    return removed


def dedupe_waiting_list(existing_concept_keys: set[str], path: Path | None = None) -> list[str]:
    """Drop duplicate waiting-list lines and entries that already have a concept page."""
    target = path or WAITING_LIST_FILE
    if not target.exists():
        return []
    text = target.read_text(encoding="utf-8")
    kept: list[str] = []
    seen_slugs: set[str] = set()
    removed: list[str] = []
    for line in text.splitlines():
        parsed = _parse_waiting_line(line)
        if not parsed:
            kept.append(line)
            continue
        slug = parsed["slug"]
        if slug in existing_concept_keys or slug in seen_slugs:
            removed.append(parsed["name"])
            continue
        seen_slugs.add(slug)
        kept.append(line)
    if not removed:
        return []
    new_text = _bump_updated_date("\n".join(kept).rstrip("\n") + "\n", today_iso())
    target.write_text(new_text, encoding="utf-8")
    return removed


def _generated_by(fm: str) -> str:
    """Return the `generated.by` writer stamp from frontmatter, inline or block form."""
    lines = fm.splitlines()
    for i, line in enumerate(lines):
        if not re.match(r"^generated:", line):
            continue
        block = [line]
        for follow in lines[i + 1 :]:
            if follow and not follow.startswith((" ", "\t")):
                break
            block.append(follow)
        m = re.search(r"\bby:\s*[\"']?([^\"',}\s]+)", "\n".join(block))
        return m.group(1) if m else ""
    return ""


def written_by_another_tool(text: str) -> str | None:
    """Say why a concept page is not this compiler's to judge, or None if it is.

    Other writers (the research processor, a session, a person) put pages in
    `concepts/` too, and one can land while a compile is running. The twice rule
    is about ideas this compiler lifts out of daily logs, so a page is left alone
    when any of these hold:
    - its `generated.by` stamp names a writer other than this compiler
    - it carries `research_items` (the research processor's link back to a receipt)
    - it lists sources and none of them is a daily log
    A page with no stamp and no sources at all is still treated as the compiler's.
    """
    fm, _ = _split_frontmatter(text)
    if not fm:
        return None
    writer = _generated_by(fm)
    if writer and not writer.startswith(COMPILER_WRITER_PREFIX):
        return f"generated by {writer}"
    if re.search(r"^research_items:", fm, re.M):
        return "has research_items"
    if _frontmatter_list(fm, "sources") and not extract_daily_log_sources(text):
        return "no daily-log source"
    return None


def snapshot_concept_files() -> set[Path]:
    """Return the set of concept page paths currently on disk (excluding index.md)."""
    if not CONCEPTS_DIR.exists():
        return set()
    return {p for p in CONCEPTS_DIR.glob("*.md") if p.name != "index.md"}


def enforce_twice_rule(pre_existing: set[Path], log_path: Path) -> dict[str, list[str]]:
    """Deterministic post-pass enforcing the twice rule on newly created concept pages.

    Existing concept pages (`pre_existing`) are never removed or rejected.
    Neither is a new page another tool wrote while this compile ran (see
    `written_by_another_tool`): it is kept, reported as `foreign_kept`, and its
    names count as existing so a compiler page cannot duplicate it.
    For every other newly created concept page:
    - If it duplicates an existing concept's slug or title, delete it.
    - If its frontmatter `sources` lists fewer than 2 distinct daily logs,
      move its gist to `WAITING_LIST_FILE`, delete the page, and record it.
    - If it has 2+ distinct daily logs, keep it and clear any matching line
      from `WAITING_LIST_FILE`.
    """
    existing_keys: set[str] = set()
    for p in pre_existing:
        if p.exists() and p.name != "index.md":
            meta = _extract_article_meta(p, p.read_text(encoding="utf-8", errors="replace"))
            existing_keys.update(meta["match_keys"])

    new_concepts = [
        p
        for p in (sorted(CONCEPTS_DIR.glob("*.md")) if CONCEPTS_DIR.exists() else [])
        if p.name != "index.md" and p not in pre_existing
    ]

    rejected: list[str] = []
    duplicates_rejected: list[str] = []
    promoted_cleared: list[str] = []
    foreign_kept: list[str] = []

    # Pages from other writers first, so their names are known before any
    # compiler page is checked against them.
    own_concepts: list[Path] = []
    for article in new_concepts:
        text = article.read_text(encoding="utf-8", errors="replace")
        why = written_by_another_tool(text)
        if why is None:
            own_concepts.append(article)
            continue
        existing_keys.update(_extract_article_meta(article, text)["match_keys"])
        foreign_kept.append(f"{article.relative_to(KNOWLEDGE_DIR)} ({why})")

    for article in own_concepts:
        text = article.read_text(encoding="utf-8", errors="replace")
        meta = _extract_article_meta(article, text)
        rel = str(article.relative_to(KNOWLEDGE_DIR))

        primary_keys = {k for k in (slugify(article.stem), slugify(meta["title"])) if k}
        if primary_keys & existing_keys:
            article.unlink()
            duplicates_rejected.append(rel)
            continue

        daily_sources = extract_daily_log_sources(text)
        if len(daily_sources) < 2:
            source = daily_sources[0] if daily_sources else f"daily/{log_path.name}"
            date = meta["created"]
            if not ISO_DATE_RE.fullmatch(date):
                m_date = ISO_DATE_RE.search(source)
                date = m_date.group(1) if m_date else today_iso()
            add_waiting_list_entry(meta["title"], meta["gist"], source, date)
            article.unlink()
            rejected.append(rel)
            continue

        removed = remove_waiting_list_entries(meta["match_keys"])
        promoted_cleared.extend(removed)
        existing_keys.update(meta["match_keys"])

    dedupe_removed = dedupe_waiting_list(existing_keys)

    if rejected and LOG_FILE.exists():
        log_text = LOG_FILE.read_text(encoding="utf-8")
        if not log_text.endswith("\n"):
            log_text += "\n"
        log_text += f"- Twice-rule rejected (moved to waiting list): {', '.join(rejected)}\n"
        LOG_FILE.write_text(log_text, encoding="utf-8")

    return {
        "rejected": rejected,
        "duplicates_rejected": duplicates_rejected,
        "promoted_cleared": promoted_cleared,
        "dedupe_removed": dedupe_removed,
        "foreign_kept": foreign_kept,
    }


def build_prompt(
    log_path: Path,
    log_content: str,
    schema: str,
    wiki_index: str,
    waiting_list: str | None = None,
    *,
    twice_rule: bool | None = None,
) -> str:
    timestamp = now_iso()
    date = timestamp[:10]
    generated_at = now_utc_iso()
    use_twice_rule = twice_rule_enabled() if twice_rule is None else twice_rule

    if not use_twice_rule:
        return f"""You are a knowledge compiler. Your job is to read a daily conversation log
and extract knowledge into structured wiki articles.

## Schema (AGENTS.md)

{schema}

## Current Wiki Index

The index below lists every existing article as `* [Title](path.md) - description`.
Use it to decide whether a concept already exists. To read an existing article,
use the Read tool on `{KNOWLEDGE_DIR}/<path>`; to find candidates by keyword, use
Grep over `{KNOWLEDGE_DIR}/concepts` and `{KNOWLEDGE_DIR}/connections`. Do not
read every article - read the handful that the log plausibly touches.

{wiki_index}

## Daily Log to Compile

**File:** {log_path.name}

{log_content}

## Your Task

Read the daily log above and compile it into wiki articles following the schema exactly.

### Rules:

1. **Extract key concepts** - Identify 3-7 distinct concepts worth their own article.
   If the log contains nothing durable (a FLUSH_OK stub, routine chatter), write
   nothing except the log.md entry and say so.
2. **Create concept articles** in `{CONCEPTS_DIR}` - One .md file per concept
   - Use the exact article format from AGENTS.md (YAML frontmatter + sections)
   - Frontmatter MUST include all of: `type: concept`, `title`, `tags`, `sources`,
     `created`, `updated`, `status: draft`, and
     `generated: {{ by: "{COMPILER_ACTOR}", at: {generated_at} }}`
   - `sources:` points to the daily log file (`daily/{log_path.name}`)
   - Use `[[concepts/slug]]` wikilinks to link to related concepts
   - Prefer tags that already appear in the index; 2-5 tags per page
   - Write in encyclopedia style - neutral, comprehensive
3. **Create connection articles** in `{CONNECTIONS_DIR}` (`type: connection`) if this
   log reveals non-obvious relationships between 2+ existing concepts
4. **Update existing articles** if this log adds new information to concepts already in the wiki
   - Read the existing article, add the new information, add the source to frontmatter,
     bump `updated`, and keep every existing frontmatter field (add `type` if it is missing)
5. **Do NOT write or edit `index.md` anywhere.** The index is regenerated by a separate
   tool after you finish. Editing it by hand is a conformance violation.
6. **Append to `{LOG_FILE}`** - Add an entry at the END of the file under a plain
   ISO date heading, exactly this shape:
   ```
   ## {date}

   - {timestamp} compile | {log_path.name}
   - Source: daily/{log_path.name}
   - Articles created: [[concepts/x]], [[concepts/y]]
   - Articles updated: [[concepts/z]] (if any)
   ```
   Use the Edit tool or Write with the full existing content plus your entry; never
   truncate log.md. Repeating a date heading for a second run on the same day is fine.
   Never use bracketed headings like `## [{timestamp}]`.

### Quality standards:
- Every article must have complete YAML frontmatter (see rule 2)
- Every article must link to at least 2 other articles via [[wikilinks]]
- Key Points section should have 3-5 bullet points
- Details section should have 2+ paragraphs
- Related Concepts section should have 2+ entries
- Sources section should cite the daily log with specific claims extracted
"""

    waiting_text = read_waiting_list() if waiting_list is None else waiting_list
    return f"""You are a knowledge compiler. Your job is to read a daily conversation log
and compile durable knowledge into the wiki following the twice rule.

## Schema (AGENTS.md)

{schema}

## Current Wiki Index

The index below lists every existing article as `* [Title](path.md) - description`.
Use it to decide whether a concept already exists. To read an existing article,
use the Read tool on `{KNOWLEDGE_DIR}/<path>`; to find candidates by keyword, use
Grep over `{KNOWLEDGE_DIR}/concepts` and `{KNOWLEDGE_DIR}/connections`. Do not
read every article - read the handful that the log plausibly touches.

{wiki_index}

## Concept Waiting List ({WAITING_LIST_FILE})

First sightings live here until they show up again in a different daily log. A line
here is not a wiki page.

{waiting_text}

## Daily Log to Compile

**File:** {log_path.name}

{log_content}

## Your Task

Read the daily log above and compile durable knowledge following the schema and the
twice rule. Zero new concept pages from a daily log is a normal result.

### Rules:

1. **Apply the twice rule to every durable idea in the log** - Do not force a quota of
   new pages. If the log contains nothing durable (a FLUSH_OK stub, routine chatter),
   write nothing except the log.md entry and say so.
   - **Existing concept page:** If an idea already has a concept page in `{CONCEPTS_DIR}`
     (check the index and Grep), UPDATE that existing page with the new information, add
     `daily/{log_path.name}` to its `sources:`, bump `updated`, and keep every existing
     frontmatter field (add `type` if missing). Never create a second page for it.
   - **Second sighting (different daily log):** If the idea has no existing page, check
     the Concept Waiting List above. If it already has a line whose `source:` is a
     DIFFERENT daily log than `daily/{log_path.name}`, CREATE a new concept article in
     `{CONCEPTS_DIR}`, cite BOTH daily logs in `sources:` (and in `## Sources`), and
     REMOVE that line from `{WAITING_LIST_FILE}`.
   - **Same-log repeat:** If the idea is already on `{WAITING_LIST_FILE}` with the SAME
     source (`daily/{log_path.name}`), do NOT create a concept page and do NOT add a
     duplicate line.
   - **First sighting:** If the idea has no existing page and no waiting-list entry, do
     NOT create a concept page. Instead, add ONE line under `## Candidates` in
     `{WAITING_LIST_FILE}` in this exact format:
     `- **Short Name** | One-sentence gist. | source: daily/{log_path.name} | date: {date}`
2. **Format for newly created concept articles** in `{CONCEPTS_DIR}` (second sightings only):
   - Use the exact article format from AGENTS.md (YAML frontmatter + sections)
   - Frontmatter MUST include all of: `type: concept`, `title`, `tags`, `sources` (with
     at least 2 distinct `daily/...` logs), `created`, `updated`, `status: draft`, and
     `generated: {{ by: "{COMPILER_ACTOR}", at: {generated_at} }}`
   - A deterministic post-pass enforces this rule: any newly created concept page whose
     `sources:` lists fewer than 2 distinct daily logs will be rejected and moved back
     to `{WAITING_LIST_FILE}`
   - Use `[[concepts/slug]]` wikilinks to link to related concepts
   - Prefer tags that already appear in the index; 2-5 tags per page
   - Write in encyclopedia style - neutral, comprehensive
3. **Create connection articles** in `{CONNECTIONS_DIR}` (`type: connection`) if this
   log reveals non-obvious relationships between 2+ existing concepts
4. **Do NOT write or edit `index.md` anywhere.** The index is regenerated by a separate
   tool after you finish. Editing it by hand is a conformance violation.
5. **Append to `{LOG_FILE}`** - Add an entry at the END of the file under a plain
   ISO date heading, exactly this shape:
   ```
   ## {date}

   - {timestamp} compile | {log_path.name}
   - Source: daily/{log_path.name}
   - Articles created: [[concepts/x]] (or none)
   - Articles updated: [[concepts/z]] (if any)
   - Waiting list added: Short Name (if any)
   ```
   Use the Edit tool or Write with the full existing content plus your entry; never
   truncate log.md. Repeating a date heading for a second run on the same day is fine.
   Never use bracketed headings like `## [{timestamp}]`.

### Quality standards for created or updated concept articles:
- Every article must have complete YAML frontmatter (see rule 2)
- Every article must link to at least 2 other articles via [[wikilinks]]
- Key Points section should have 3-5 bullet points
- Details section should have 2+ paragraphs
- Related Concepts section should have 2+ entries
- Sources section should cite each contributing daily log with specific claims extracted
"""


def ensure_okf_frontmatter(since: float) -> list[str]:
    """Deterministic post-pass: every article touched since `since` gets a
    non-empty `type` (from its directory) and a `generated` field if missing.
    The prompt asks for both; this makes the contract hold even when the model
    forgets. Returns the paths it repaired."""
    repaired: list[str] = []
    for article in list_wiki_articles():
        if article.name == "index.md":
            continue
        if article.stat().st_mtime < since:
            continue
        text = article.read_text(encoding="utf-8")
        m = FM_RE.match(text)
        if not m:
            kind = article.parent.name.rstrip("s") if article.parent.name != "qa" else "qa"
            text = (
                f"---\ntype: {kind}\ntitle: {article.stem}\n"
                f'generated: {{ by: "{COMPILER_ACTOR}", at: {now_utc_iso()} }}\n---\n' + text
            )
            article.write_text(text, encoding="utf-8")
            repaired.append(str(article.relative_to(KNOWLEDGE_DIR)))
            continue
        fm = m.group(1)
        additions = []
        if not TYPE_RE.search(fm):
            kind = {"concepts": "concept", "connections": "connection", "qa": "qa"}.get(
                article.parent.name, "concept"
            )
            additions.append(f"type: {kind}")
        if not GENERATED_RE.search(fm):
            additions.append(f'generated: {{ by: "{COMPILER_ACTOR}", at: {now_utc_iso()} }}')
        if additions:
            new_fm = fm + "\n" + "\n".join(additions)
            article.write_text(text[: m.start(1)] + new_fm + text[m.end(1) :], encoding="utf-8")
            repaired.append(str(article.relative_to(KNOWLEDGE_DIR)))
    return repaired


def normalize_log_headings() -> int:
    """Rewrite any `## [YYYY-MM-DD...]` heading in log.md to `## YYYY-MM-DD`."""
    if not LOG_FILE.exists():
        return 0
    text = LOG_FILE.read_text(encoding="utf-8")
    new_text, n = BRACKET_HEADING_RE.subn(r"## \1", text)
    if n:
        LOG_FILE.write_text(new_text, encoding="utf-8")
    return n


def reindex() -> str:
    """Defer index.md generation to the vault's okf_reindex.py (single writer)."""
    if not OKF_REINDEX.exists():
        return f"  Index: {OKF_REINDEX} not found; index.md left untouched"
    result = subprocess.run(
        [sys.executable, "-I", str(OKF_REINDEX)],
        capture_output=True,
        text=True,
        cwd=str(KNOWLEDGE_DIR),
    )
    first = (result.stdout or result.stderr).strip().splitlines()[:1]
    return f"  Index: okf_reindex.py exit {result.returncode} {first[0] if first else ''}"


class UsageLimitHit(Exception):
    """The Claude Max usage window is exhausted; nothing was written."""

    def __init__(self, resets_at: int | None):
        super().__init__(f"usage limit hit, resets_at={resets_at or ''}")
        self.resets_at = resets_at


async def compile_daily_log(log_path: Path, state: dict, query_fn=None) -> float:
    """Compile a single daily log into knowledge articles.

    Returns the SDK-reported cost (notional on a Max login - nothing is billed).
    Raises UsageLimitHit when the CLI reports the usage window is exhausted.
    """
    use_twice = twice_rule_enabled()
    if use_twice:
        ensure_waiting_list()

    log_content = log_path.read_text(encoding="utf-8")
    schema = AGENTS_FILE.read_text(encoding="utf-8")
    wiki_index = read_wiki_index()
    waiting_text = read_waiting_list() if use_twice else None
    prompt = build_prompt(
        log_path,
        log_content,
        schema,
        wiki_index,
        waiting_list=waiting_text,
        twice_rule=use_twice,
    )

    pre_existing_concepts = snapshot_concept_files()
    cost = 0.0
    started = time.time()
    usage: dict = {}
    error = None
    limit_hit = False
    resets_at = None

    if query_fn is None:
        from claude_agent_sdk import RateLimitEvent, ResultMessage, query

        active_query = query
        options = lean_sdk_options(
            cwd=str(VAULT_DIR),
            add_dirs=[str(KNOWLEDGE_DIR)],
            system_prompt=(
                "You are a careful, terse knowledge-base compiler. You write files "
                "with the tools you are given and report what you wrote in one paragraph."
            ),
            allowed_tools=["Read", "Write", "Edit", "Glob", "Grep"],
            permission_mode="acceptEdits",
            max_turns=40,
            model=COMPILE_MODEL,
        )
    else:
        RateLimitEvent = ()  # type: ignore[assignment]
        ResultMessage = ()  # type: ignore[assignment]
        active_query = query_fn
        options = None

    try:
        async for message in active_query(prompt=prompt, options=options):
            if RateLimitEvent and isinstance(message, RateLimitEvent):
                info = message.rate_limit_info
                if info.status == "rejected":
                    limit_hit = True
                    resets_at = info.resets_at
            elif ResultMessage and isinstance(message, ResultMessage):
                cost = message.total_cost_usd or 0.0
                usage = message.usage or {}
                if message.is_error:
                    text = (message.result or "")[:200]
                    if LIMIT_TEXT_RE.search(text):
                        limit_hit = True
                    error = f"result is_error subtype={message.subtype} {text}".strip()
            elif hasattr(message, "total_cost_usd"):
                cost = getattr(message, "total_cost_usd", 0.0) or 0.0
                usage = getattr(message, "usage", {}) or {}
                if getattr(message, "is_error", False):
                    text = (getattr(message, "result", "") or "")[:200]
                    if LIMIT_TEXT_RE.search(text):
                        limit_hit = True
                    error = f"result is_error {text}".strip()
    except Exception as e:  # noqa: BLE001 - one line in the log is the contract
        error = f"{type(e).__name__}: {e}"

    elapsed = time.time() - started
    if limit_hit:
        print(f"  PAUSED: usage limit hit, resets_at={resets_at or ''} ({elapsed:.0f}s)")
        raise UsageLimitHit(resets_at)
    if error:
        print(f"  ERROR: {error} ({elapsed:.0f}s)")
        return 0.0

    twice_result = enforce_twice_rule(pre_existing_concepts, log_path) if use_twice else None
    repaired = ensure_okf_frontmatter(started)
    fixed_headings = normalize_log_headings()
    print(
        f"  Cost: ${cost:.4f} (notional, Max login)  time: {elapsed:.0f}s  "
        f"in={usage.get('input_tokens', 0)} cache_create={usage.get('cache_creation_input_tokens', 0)} "
        f"cache_read={usage.get('cache_read_input_tokens', 0)} out={usage.get('output_tokens', 0)}"
    )
    if twice_result:
        if twice_result["rejected"]:
            print(
                f"  Twice rule: rejected {len(twice_result['rejected'])} single-source "
                f"concept(s) -> waiting list: {', '.join(twice_result['rejected'])}"
            )
        if twice_result["duplicates_rejected"]:
            print(
                f"  Twice rule: rejected {len(twice_result['duplicates_rejected'])} duplicate "
                f"concept(s): {', '.join(twice_result['duplicates_rejected'])}"
            )
        if twice_result["promoted_cleared"]:
            print(
                f"  Twice rule: cleared {len(twice_result['promoted_cleared'])} waiting-list "
                f"entry(ies): {', '.join(twice_result['promoted_cleared'])}"
            )
        if twice_result["foreign_kept"]:
            print(
                f"  Twice rule: left {len(twice_result['foreign_kept'])} page(s) from another "
                f"writer alone: {', '.join(twice_result['foreign_kept'])}"
            )
    if repaired:
        print(f"  Frontmatter repaired on {len(repaired)}: {', '.join(repaired)}")
    if fixed_headings:
        print(f"  log.md: rewrote {fixed_headings} bracketed heading(s)")

    state.setdefault("ingested", {})[log_path.name] = {
        "hash": file_hash(log_path),
        "compiled_at": now_iso(),
        "cost_usd": cost,
        "seconds": round(elapsed),
        "model": COMPILE_MODEL,
    }
    state["total_cost"] = state.get("total_cost", 0.0) + cost
    save_state(state)
    return cost


def main():
    parser = argparse.ArgumentParser(description="Compile daily logs into knowledge articles")
    parser.add_argument("--all", action="store_true", help="Force recompile all logs")
    parser.add_argument("--file", type=str, help="Compile a specific daily log file")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be compiled")
    args = parser.parse_args()

    state = load_state()

    if args.file:
        target = Path(args.file)
        if not target.is_absolute():
            target = DAILY_DIR / target.name
        if not target.exists():
            target = ROOT_DIR / args.file
        if not target.exists():
            print(f"Error: {args.file} not found")
            sys.exit(1)
        to_compile = [target]
    else:
        all_logs = list_raw_files()
        if args.all:
            to_compile = all_logs
        else:
            to_compile = []
            for log_path in all_logs:
                prev = state.get("ingested", {}).get(log_path.name, {})
                if not prev or prev.get("hash") != file_hash(log_path):
                    to_compile.append(log_path)

    if not to_compile:
        print("Nothing to compile - all daily logs are up to date.")
        return

    print(
        f"{'[DRY RUN] ' if args.dry_run else ''}{today_iso()} vault={VAULT_DIR} "
        f"model={COMPILE_MODEL} twice_rule={'on' if twice_rule_enabled() else 'off'}"
    )
    print(f"Files to compile ({len(to_compile)}):")
    for f in to_compile:
        print(f"  - {f.name}")

    if args.dry_run:
        return

    total_cost = 0.0
    paused: UsageLimitHit | None = None
    for i, log_path in enumerate(to_compile, 1):
        print(f"\n[{i}/{len(to_compile)}] Compiling {log_path.name}...")
        try:
            total_cost += asyncio.run(compile_daily_log(log_path, state))
        except UsageLimitHit as hit:
            paused = hit
            break

    print(reindex())
    articles = list_wiki_articles()
    print(
        f"\nCompilation {'paused' if paused else 'complete'}. Total cost: ${total_cost:.2f} (notional)"
    )
    print(f"Knowledge base: {len(articles)} articles")
    if paused:
        # Exit 3 so a driver (scripts/backfill.py) can wait for the window and resume.
        print(f"PAUSED resets_at={paused.resets_at or ''}")
        sys.exit(3)


if __name__ == "__main__":
    main()
