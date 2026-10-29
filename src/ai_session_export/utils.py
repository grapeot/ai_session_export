from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path


SUBAGENT_PATTERNS = (
    "@explore subagent",
    "@librarian subagent",
    "@oracle subagent",
    "@general subagent",
    "@hephaestus subagent",
    "@metis subagent",
    "@momus subagent",
)
SYSTEM_PREFIXES = ("find ", "search ", "explore ")


def sanitize_filename(title: str, max_length: int = 80) -> str:
    text = (title or "").strip()
    text = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    if not text:
        text = "untitled"
    return text[:max_length]


def yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def unique_output_path(output_dir: Path, date_ymd: str, title: str) -> Path:
    prefix = date_ymd.replace("-", "")
    stem = f"{prefix}_{sanitize_filename(title)}"
    candidate = output_dir / f"{stem}.md"
    counter = 2
    while candidate.exists():
        candidate = output_dir / f"{stem}_{counter}.md"
        counter += 1
    return candidate


def archived_session_id(path: Path, max_head_bytes: int = 65536) -> str | None:
    """Return the session_id recorded in a rendered archive file, if parseable.

    Archive files carry their session id in the YAML frontmatter, a few lines
    from the top. The frontmatter itself grows with the session (per-turn
    ``turn_models`` lists), so the closing ``---`` can sit beyond any fixed
    window: when the close marker is outside the head slice, treat the slice
    as still-frontmatter instead of failing to parse. Files that do not parse
    as an archive return None; callers must treat None as a foreign session
    and never overwrite the file.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            head = handle.read(max_head_bytes)
    except OSError:
        return None
    if not head.startswith("---"):
        return None
    close = head.find("\n---", 3)
    window = head[:close] if close != -1 else head
    match = re.search(r"^session_id:\s*(.+)$", window, re.MULTILINE)
    if not match:
        return None
    try:
        return str(json.loads(match.group(1).strip()))
    except json.JSONDecodeError:
        return match.group(1).strip().strip('"')


def claim_archive_file(output_dir: Path, session_id: str, prefix: str | None = None) -> Path | None:
    """Find a session's archive file inside ``output_dir`` by frontmatter id.

    Used when the stem chain (creation date + current title) holds no match,
    for example after the session was renamed since its first export. Returns
    the file for in-place overwrite, without renaming; None when nothing
    claims it. Same-day files are probed first.
    """
    try:
        candidates = sorted(
            (item for item in output_dir.iterdir() if item.is_file() and item.name.endswith(".md")),
            key=lambda item: item.name,
        )
    except OSError:
        return None
    if prefix:
        same_day = [item for item in candidates if item.name.startswith(prefix)]
        others = [item for item in candidates if not item.name.startswith(prefix)]
    else:
        same_day, others = candidates, []
    for item in same_day + others:
        if archived_session_id(item) == session_id:
            return item
    return None


def session_aware_output_path(
    output_dir: Path,
    session_id: str,
    date_ymd: str,
    title: str,
    max_candidates: int = 512,
) -> Path:
    """Resolve the archive path for a session that may already be exported.

    Sessions are long-lived and keep growing after their first export.
    Re-emitting one must update the file it already owns (matched by
    frontmatter ``session_id``), not create an ``_2`` duplicate. The search
    walks the numbering chain implied by :func:`unique_output_path`; every
    returned path is either a confirmed same-session file or a non-existing
    create path. If the chain runs out without a match, claim the session's
    file by its frontmatter id anywhere in the directory, then fall back to
    the next guaranteed-new number.
    """
    prefix = date_ymd.replace("-", "")
    stem = f"{prefix}_{sanitize_filename(title)}"
    candidate = output_dir / f"{stem}.md"
    counter = 2
    while counter <= max_candidates:
        if not candidate.exists():
            return candidate
        if archived_session_id(candidate) == session_id:
            return candidate
        candidate = output_dir / f"{stem}_{counter}.md"
        counter += 1
    claimed = claim_archive_file(output_dir, session_id, prefix=prefix)
    if claimed is not None:
        return claimed
    fallback = max_candidates + 1
    candidate = output_dir / f"{stem}_{fallback}.md"
    while candidate.exists():
        fallback += 1
        candidate = output_dir / f"{stem}_{fallback}.md"
    return candidate


def should_skip_session(title: str | None) -> bool:
    text = (title or "").strip().lower()
    if not text:
        return False
    if any(pattern in text for pattern in SUBAGENT_PATTERNS):
        return True
    if text.startswith(SYSTEM_PREFIXES) and "subagent" in text:
        return True
    return False


def parse_second_mind_date(created_at: str) -> str:
    try:
        dt = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S.%f")
    except ValueError:
        dt = datetime.fromisoformat(created_at)
    return dt.date().isoformat()


def ms_to_date(epoch_ms: int) -> str:
    return datetime.fromtimestamp(epoch_ms / 1000).date().isoformat()


def ms_to_hhmm(epoch_ms: int) -> str:
    """Local-time HH:MM for a ms epoch (used in per-turn markdown headers)."""
    return datetime.fromtimestamp(epoch_ms / 1000).strftime("%H:%M")


def to_local_date(value: datetime) -> date:
    """Session date in local time, matching the local HH:MM turn headers.

    Agent transcripts usually store timestamps as UTC (``...Z``). Using the UTC
    date while rendering local turn times files evening sessions under the next
    day, so downstream window filters (e.g. eink_diary) replay yesterday's work.
    Naive values are treated as already-local wall-clock time.
    """
    if value.tzinfo is not None:
        return value.astimezone().date()
    return value.date()


def resolve_output_path(
    output_dir: Path, previous_output: str, date_ymd: str, title: str
) -> Path:
    """Keep an existing archive file, renaming it when its date prefix is stale."""
    if previous_output:
        previous = output_dir / Path(previous_output).name
        if not previous.is_file():
            return previous
        expected_prefix = date_ymd.replace("-", "") + "_"
        if previous.name.startswith(expected_prefix):
            return previous
        renamed = unique_output_path(output_dir, date_ymd, title)
        previous.replace(renamed)
        return renamed
    return unique_output_path(output_dir, date_ymd, title)


def parse_iso_timestamp(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def date_from_cli(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()
