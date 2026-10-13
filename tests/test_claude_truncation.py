"""Claude Code truncation detection and cloud backfill merge (synthetic fixtures only)."""

from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import ai_session_export.cli as cli_module
from ai_session_export.cli import run_export
from ai_session_export.markdown import render_markdown
from ai_session_export.models import MessageTurn, SessionRecord
from ai_session_export.sources.claude_code import export_claude_code, parse_claude_session_file


SESSION_ID = "claude-trunc-fixture"
PROJECT = "-home-example-project"
SUMMARY = (
    "This session is being continued from a previous conversation that ran out of context. "
    "The conversation is summarized below: fixture summary."
)
NEW_KEYS = ("transcript_truncated", "truncation_signals", "earliest_known_activity", "backfilled_messages")


def _event(kind: str, uuid: str, parent: str | None, timestamp: str, content, **extra) -> dict:
    message = {"role": kind, "content": content}
    if kind == "assistant":
        message["model"] = "fixture-model"
    return {
        "type": kind,
        "uuid": uuid,
        "parentUuid": parent,
        "timestamp": timestamp,
        "sessionId": SESSION_ID,
        "cwd": "/home/example/project",
        "isSidechain": False,
        "message": message,
        **extra,
    }


def _write_session(
    projects_root: Path,
    *,
    first_parent: str | None = None,
    first_user_text: str = "Plan the fixture feature",
    subagent_first_timestamps: list[str] | None = None,
) -> Path:
    project_dir = projects_root / PROJECT
    project_dir.mkdir(parents=True, exist_ok=True)
    session_file = project_dir / f"{SESSION_ID}.jsonl"
    events = [
        {"type": "summary", "summary": "fixture summary record", "leafUuid": "u-4"},
        _event("user", "u-1", first_parent, "2026-07-02T10:00:00Z", first_user_text),
        _event("assistant", "u-2", "u-1", "2026-07-02T10:01:00Z", [{"type": "text", "text": "Fixture plan ready."}]),
        _event("user", "u-3", "u-2", "2026-07-02T10:02:00Z", "Implement the fixture plan"),
        _event("assistant", "u-4", "u-3", "2026-07-02T10:03:00Z", "Fixture implemented."),
    ]
    session_file.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
    if subagent_first_timestamps is not None:
        subagent_dir = project_dir / SESSION_ID / "subagents"
        subagent_dir.mkdir(parents=True, exist_ok=True)
        for index, first_ts in enumerate(subagent_first_timestamps):
            lines = [
                "not json",
                json.dumps({"type": "user", "uuid": f"s{index}-0"}),
                json.dumps({"type": "user", "uuid": f"s{index}-1", "timestamp": first_ts, "isSidechain": True}),
                # Later lines are never needed; an unparseable tail must not matter.
                "{broken",
            ]
            (subagent_dir / f"agent-{index}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return session_file


def _write_backfill(backfill_dir: Path, rows: list[dict], session_id: str = SESSION_ID) -> Path:
    backfill_dir.mkdir(parents=True, exist_ok=True)
    path = backfill_dir / f"{session_id}.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path


def _frontmatter(text: str) -> dict[str, str]:
    lines = text.split("\n")
    assert lines[0] == "---"
    end = lines.index("---", 1)
    return dict(line.split(": ", 1) for line in lines[1:end])


def _export(tmp_path: Path, **overrides):
    kwargs = dict(
        full=False,
        dry_run=False,
        since_date=None,
        project_dirs=(tmp_path / "projects",),
        history_files=(tmp_path / "absent-history.jsonl",),
        backfill_dir=tmp_path / "backfill",
    )
    kwargs.update(overrides)
    state = kwargs.pop("state", None)
    state = {} if state is None else state
    return export_claude_code(tmp_path / "out", state, **kwargs), state


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #


def test_no_signals_leaves_output_unchanged(tmp_path: Path) -> None:
    session_file = _write_session(
        tmp_path / "projects", subagent_first_timestamps=["2026-07-02T10:01:30Z"]
    )
    parsed = parse_claude_session_file(session_file, {})
    assert parsed is not None
    record = parsed.record
    assert record.truncation_signals == []
    assert record.earliest_known_activity == ""
    assert record.backfilled_messages == 0
    assert parsed.warnings == []

    legacy = SessionRecord(
        source=record.source,
        session_id=record.session_id,
        title=record.title,
        date=record.date,
        messages=record.messages,
        project_directory=record.project_directory,
        models_used=record.models_used,
    )
    output = render_markdown(record)
    assert output == render_markdown(legacy)
    assert not any(key in output for key in NEW_KEYS)

    result, _ = _export(tmp_path)
    assert result["exported"] == 1
    assert "warnings" not in result


def test_parent_uuid_missing_signal(tmp_path: Path) -> None:
    session_file = _write_session(tmp_path / "projects", first_parent="u-not-in-file")
    parsed = parse_claude_session_file(session_file, {})
    assert parsed is not None
    assert parsed.record.truncation_signals == ["parent_uuid_missing"]
    assert parsed.record.earliest_known_activity == ""
    meta = _frontmatter(render_markdown(parsed.record))
    assert meta["transcript_truncated"] == "true"
    assert json.loads(meta["truncation_signals"]) == ["parent_uuid_missing"]
    assert "earliest_known_activity" not in meta


def test_parent_uuid_present_in_file_is_not_a_signal(tmp_path: Path) -> None:
    session_file = _write_session(tmp_path / "projects", first_parent="u-4")
    parsed = parse_claude_session_file(session_file, {})
    assert parsed is not None
    assert parsed.record.truncation_signals == []


def test_continuation_summary_signal(tmp_path: Path) -> None:
    session_file = _write_session(tmp_path / "projects", first_user_text=SUMMARY)
    parsed = parse_claude_session_file(session_file, {})
    assert parsed is not None
    assert parsed.record.truncation_signals == ["starts_with_continuation_summary"]


def test_continuation_summary_later_in_file_is_not_a_signal(tmp_path: Path) -> None:
    session_file = _write_session(tmp_path / "projects")
    with session_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_event("user", "u-5", "u-4", "2026-07-02T11:00:00Z", SUMMARY)) + "\n")
    parsed = parse_claude_session_file(session_file, {})
    assert parsed is not None
    assert parsed.record.truncation_signals == []


def test_subagent_activity_before_transcript_signal(tmp_path: Path) -> None:
    session_file = _write_session(
        tmp_path / "projects",
        subagent_first_timestamps=["2026-07-02T10:05:00Z", "2026-06-28T08:30:00Z", "2026-06-30T12:00:00Z"],
    )
    parsed = parse_claude_session_file(session_file, {})
    assert parsed is not None
    assert parsed.record.truncation_signals == ["subagent_activity_before_transcript"]
    assert parsed.record.earliest_known_activity == "2026-06-28T08:30:00Z"
    meta = _frontmatter(render_markdown(parsed.record))
    assert meta["earliest_known_activity"] == '"2026-06-28T08:30:00Z"'
    # Frontmatter date still reflects the local transcript until a backfill exists.
    assert meta["date"] == '"2026-07-02"'


def test_all_signals_and_export_warning(tmp_path: Path) -> None:
    _write_session(
        tmp_path / "projects",
        first_parent="u-missing",
        first_user_text=SUMMARY,
        subagent_first_timestamps=["2026-06-28T08:30:00Z"],
    )
    result, _ = _export(tmp_path)
    assert result["exported"] == 1
    [warning] = result["warnings"]
    assert warning["session_id"] == SESSION_ID
    assert "2026-07-02T10:00:00Z" in warning["error"]
    assert (
        "parent_uuid_missing, starts_with_continuation_summary, subagent_activity_before_transcript"
        in warning["error"]
    )
    assert 'skill.md "Recovering a truncated Claude Code Remote session"' in warning["error"]
    content = next((tmp_path / "out").glob("*.md")).read_text(encoding="utf-8")
    meta = _frontmatter(content)
    assert meta["transcript_truncated"] == "true"
    assert len(json.loads(meta["truncation_signals"])) == 3


# --------------------------------------------------------------------------- #
# Backfill merge
# --------------------------------------------------------------------------- #


BACKFILL_ROWS = [
    {"created_at": "2026-06-28T09:10:00Z", "role": "assistant", "text": "Earlier fixture answer."},
    {"created_at": "2026-06-28T09:00:00Z", "text": "Earlier fixture question"},  # role defaults to user
    {"created_at": "2026-06-28T09:10:00Z", "role": "assistant", "text": "Earlier fixture answer."},  # dup
    {"created_at": "2026-07-02T10:00:00Z", "role": "user", "text": "Overlaps first local message"},
    {"created_at": "2026-07-02T10:02:30Z", "role": "user", "text": "Inside the local range"},
    {"created_at": "2026-06-29T00:00:00+00:00", "role": "user", "text": "Second earlier question"},
    {"created_at": "2026-06-29T00:01:00Z", "role": "user", "text": "   "},  # empty text, dropped
]


def test_backfill_merge_order_dedupe_overlap_and_date(tmp_path: Path) -> None:
    _write_session(tmp_path / "projects", first_parent="u-missing", first_user_text=SUMMARY)
    _write_backfill(tmp_path / "backfill", BACKFILL_ROWS)

    result, state = _export(tmp_path)
    assert result["exported"] == 1
    output = next((tmp_path / "out").glob("*.md"))
    content = output.read_text(encoding="utf-8")
    meta = _frontmatter(content)
    assert meta["backfilled_messages"] == "3"
    assert meta["date"] == '"2026-06-28"'
    assert meta["message_count"] == "7"
    assert meta["transcript_truncated"] == "true"
    assert json.loads(meta["truncation_signals"]) == ["parent_uuid_missing", "starts_with_continuation_summary"]
    # Without a history title, the first recovered user message names the session.
    assert meta["title"] == '"Earlier fixture question"'
    assert output.name.startswith("20260628_")

    body = content.split("\n---\n", 1)[1]
    order = [
        "Earlier fixture question",
        "Earlier fixture answer.",
        "Second earlier question",
        "This session is being continued",
        "Fixture plan ready.",
        "Implement the fixture plan",
        "Fixture implemented.",
    ]
    positions = [body.index(text) for text in order]
    assert positions == sorted(positions)
    assert body.count("Earlier fixture answer.") == 1
    assert "Overlaps first local message" not in body
    assert "Inside the local range" not in body
    assert "backfilled_messages: 3" in result["warnings"][0]["error"]

    parsed = parse_claude_session_file(
        tmp_path / "projects" / PROJECT / f"{SESSION_ID}.jsonl", {}, backfill_dir=tmp_path / "backfill"
    )
    assert parsed is not None
    roles = [turn.role for turn in parsed.record.messages[:3]]
    assert roles == ["user", "assistant", "user"]
    assert all(turn.time_created for turn in parsed.record.messages[:3])
    assert state["claude_code"]["sessions"][SESSION_ID]["backfill_mtime_ns"] > 0


def test_backfill_without_signals_still_merges(tmp_path: Path) -> None:
    _write_session(tmp_path / "projects")
    _write_backfill(tmp_path / "backfill", BACKFILL_ROWS[:2])
    result, _ = _export(tmp_path)
    assert result["exported"] == 1
    assert "warnings" not in result
    meta = _frontmatter(next((tmp_path / "out").glob("*.md")).read_text(encoding="utf-8"))
    assert meta["backfilled_messages"] == "2"
    assert "transcript_truncated" not in meta


def test_backfill_with_only_overlapping_rows_changes_nothing(tmp_path: Path) -> None:
    session_file = _write_session(tmp_path / "projects")
    _write_backfill(tmp_path / "backfill", BACKFILL_ROWS[3:5])
    plain = parse_claude_session_file(session_file, {})
    merged = parse_claude_session_file(session_file, {}, backfill_dir=tmp_path / "backfill")
    assert plain is not None and merged is not None
    assert render_markdown(merged.record) == render_markdown(plain.record)


def test_invalid_backfill_lines_are_skipped_with_warning(tmp_path: Path) -> None:
    session_file = _write_session(tmp_path / "projects")
    path = _write_backfill(tmp_path / "backfill", BACKFILL_ROWS[:2])
    with path.open("a", encoding="utf-8") as handle:
        handle.write("not json\n")
        handle.write(json.dumps({"created_at": "not a time", "text": "x"}) + "\n")
        handle.write(json.dumps({"created_at": "2026-06-28T08:00:00Z", "role": "system", "text": "x"}) + "\n")
    parsed = parse_claude_session_file(session_file, {}, backfill_dir=tmp_path / "backfill")
    assert parsed is not None
    assert parsed.record.backfilled_messages == 2
    assert parsed.warnings == [{"session_id": SESSION_ID, "error": "skipped 3 invalid backfill line(s)"}]


def test_incremental_reexport_when_only_backfill_changes(tmp_path: Path) -> None:
    _write_session(tmp_path / "projects", first_parent="u-missing")
    state: dict = {}
    first, state = _export(tmp_path, state=state)
    assert first["exported"] == 1
    output = next((tmp_path / "out").glob("*.md"))
    assert "backfilled_messages" not in output.read_text(encoding="utf-8")
    assert "backfill_mtime_ns" not in state["claude_code"]["sessions"][SESSION_ID]
    assert _export(tmp_path, state=state)[0]["exported"] == 0

    backfill = _write_backfill(tmp_path / "backfill", BACKFILL_ROWS[:1])
    second, state = _export(tmp_path, state=state)
    assert second["exported"] == 1
    assert list((tmp_path / "out").glob("*.md")) == [output]
    assert "backfilled_messages: 1" in output.read_text(encoding="utf-8")
    recorded = state["claude_code"]["sessions"][SESSION_ID]["backfill_mtime_ns"]
    assert recorded == backfill.stat().st_mtime_ns
    assert _export(tmp_path, state=state)[0]["exported"] == 0

    _write_backfill(tmp_path / "backfill", BACKFILL_ROWS[:2])
    newer = recorded + 5_000_000_000
    os.utime(backfill, ns=(newer, newer))
    third, state = _export(tmp_path, state=state)
    assert third["exported"] == 1
    assert "backfilled_messages: 2" in output.read_text(encoding="utf-8")
    assert state["claude_code"]["sessions"][SESSION_ID]["backfill_mtime_ns"] == newer
    assert list((tmp_path / "out").glob("*.md")) == [output]


def test_dry_run_with_backfill_does_not_mutate_state(tmp_path: Path) -> None:
    _write_session(tmp_path / "projects")
    _write_backfill(tmp_path / "backfill", BACKFILL_ROWS[:1])
    result, state = _export(tmp_path, dry_run=True)
    assert result["exported"] == 1
    assert state == {}
    assert not (tmp_path / "out").exists()


def test_since_date_uses_backfilled_date(tmp_path: Path) -> None:
    _write_session(tmp_path / "projects")
    _write_backfill(tmp_path / "backfill", BACKFILL_ROWS[:1])
    assert _export(tmp_path, since_date=date(2026, 6, 28))[0]["exported"] == 1
    assert _export(tmp_path, full=True, since_date=date(2026, 6, 29))[0]["exported"] == 0


# --------------------------------------------------------------------------- #
# Renderer and CLI wiring
# --------------------------------------------------------------------------- #


def test_render_markdown_truncation_keys() -> None:
    record = SessionRecord(
        source="claude_code",
        session_id="fixture",
        title="Fixture",
        date="2026-06-28",
        messages=[MessageTurn(role="user", content="Hello")],
        truncation_signals=["parent_uuid_missing"],
        earliest_known_activity="2026-06-28T08:30:00Z",
        backfilled_messages=4,
    )
    output = render_markdown(record)
    assert (
        'message_count: 1\n'
        'transcript_truncated: true\n'
        'truncation_signals: ["parent_uuid_missing"]\n'
        'earliest_known_activity: "2026-06-28T08:30:00Z"\n'
        'backfilled_messages: 4\n'
        '---\n'
    ) in output


def test_run_export_uses_default_backfill_dir_under_base_dir(tmp_path: Path) -> None:
    _write_session(tmp_path / "projects")
    _write_backfill(tmp_path / "claude_code_backfill", BACKFILL_ROWS[:2])
    results = run_export(
        "claude-code",
        full=False,
        dry_run=False,
        base_dir=tmp_path,
        state_file=tmp_path / "state.json",
        claude_project_dirs=(tmp_path / "projects",),
        claude_history_files=(tmp_path / "absent-history.jsonl",),
    )
    assert [r["source"] for r in results] == ["claude_code"]
    content = next((tmp_path / "claude_code").glob("*.md")).read_text(encoding="utf-8")
    assert "backfilled_messages: 2" in content


def test_parse_args_claude_backfill_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["export_sessions.py"])
    assert cli_module.parse_args().claude_backfill_dir is None
    monkeypatch.setattr(sys, "argv", ["export_sessions.py", "--claude-backfill-dir", "/tmp/example-backfill"])
    assert cli_module.parse_args().claude_backfill_dir == Path("/tmp/example-backfill")


def test_cli_main_wires_backfill_dir_and_prints_truncation_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_session(tmp_path / "projects", first_parent="u-missing")
    custom_backfill = tmp_path / "custom-backfill"
    _write_backfill(custom_backfill, BACKFILL_ROWS[:2])
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "export_sessions.py",
            "--source", "claude-code",
            "--base-dir", str(tmp_path / "archive"),
            "--state-file", str(tmp_path / "state.json"),
            "--claude-backfill-dir", str(custom_backfill),
        ],
    )
    captured_kwargs: dict = {}
    real_run_export = cli_module.run_export

    def fake_run_export(source, **kwargs):
        captured_kwargs.update(kwargs)
        kwargs["claude_project_dirs"] = (tmp_path / "projects",)
        kwargs["claude_history_files"] = (tmp_path / "absent-history.jsonl",)
        return real_run_export(source, **kwargs)

    monkeypatch.setattr(cli_module, "run_export", fake_run_export)
    cli_module.main()

    assert captured_kwargs["claude_backfill_dir"] == custom_backfill
    out = capsys.readouterr()
    assert "[claude_code] exported=1 scanned=1" in out.out
    warning_lines = [line for line in out.err.splitlines() if "transcript truncated" in line]
    assert len(warning_lines) == 1
    assert warning_lines[0].startswith(f"[claude_code] {SESSION_ID}: transcript truncated")
    assert "parent_uuid_missing" in warning_lines[0]
    assert "skill.md" in warning_lines[0]
    content = next((tmp_path / "archive" / "claude_code").glob("*.md")).read_text(encoding="utf-8")
    assert "backfilled_messages: 2" in content
