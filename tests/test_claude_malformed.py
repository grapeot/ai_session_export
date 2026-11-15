"""Issue #18: physical-line corruption must not stop healthy session exports."""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import ai_session_export.cli as cli
from ai_session_export.sources.claude_code import export_claude_code


def event(text: str, minute: int = 0, session_id: str = "fixture") -> bytes:
    return json.dumps({"type": "user", "sessionId": session_id,
                       "timestamp": f"2026-11-14T12:{minute:02d}:00Z",
                       "message": {"content": text}}).encode() + b"\n"


def export(tmp_path, state, **kwargs):
    return export_claude_code(tmp_path / "out", state, full=False, dry_run=False,
                              since_date=None, project_dirs=(tmp_path / "projects",),
                              history_files=kwargs.pop("history_files", ()), **kwargs)


@pytest.fixture
def transcript(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    path = root / "a.jsonl"
    path.write_bytes(event("Start"))
    return path


@pytest.mark.parametrize("bad", [b"{broken\n", b"\xff\xfe\n", b"[]\n", b"null\n", b"\x00" * 19 + b"\n"])
def test_skips_bad_line_and_continues_other_sessions(tmp_path, transcript, bad):
    transcript.write_bytes(event("Start") + bad + event("After corruption", 1))
    (transcript.parent / "b.jsonl").write_bytes(event("Healthy session", session_id="healthy"))
    result = export(tmp_path, {})
    assert result["exported"] == 2
    assert result["skipped_lines"] == 1
    assert result["failed"] == 0
    warning = result["warnings"][0]
    assert warning["path"] == str(transcript)
    assert warning["line"] == 2
    assert "After corruption" in next(p for p in (tmp_path / "out").glob("*.md") if "Start" in p.name).read_text()


def test_nul_prefix_recovers_record_without_skipping(tmp_path, transcript):
    transcript.write_bytes(event("Start") + b"\x00" * 19 + event("Recovered text", 1))
    result = export(tmp_path, {})
    assert result["recovered_lines"] == 1
    assert result["skipped_lines"] == 0
    assert "Recovered text" in next((tmp_path / "out").glob("*.md")).read_text()


def test_completely_bad_session_and_bad_history_are_visible(tmp_path, transcript):
    transcript.write_bytes(b"invalid private text\n")
    (transcript.parent / "b.jsonl").write_bytes(event("Healthy", session_id="healthy"))
    history = tmp_path / "history.jsonl"
    history.write_bytes(b"\xff\n" + json.dumps({"sessionId": "healthy", "display": "History title", "timestamp": 1}).encode() + b"\n")
    result = export(tmp_path, {}, history_files=(history,))
    assert result["exported"] == 1
    assert result["skipped_lines"] == 2
    assert "invalid private text" not in str(result)
    assert next((tmp_path / "out").glob("*.md")).name.endswith("History_title.md")


def test_partial_export_retries_repaired_line_at_same_timestamp(tmp_path, transcript):
    state = {}
    transcript.write_bytes(event("Start") + b"{broken\n" + event("Last", 2))
    export(tmp_path, state)
    assert state["claude_code"]["sessions"]["fixture"]["incomplete"]
    transcript.write_bytes(event("Start") + event("Repaired", 1) + event("Last", 2))
    result = export(tmp_path, state)
    assert result["exported"] == 1
    assert "incomplete" not in state["claude_code"]["sessions"]["fixture"]
    assert "Repaired" in next((tmp_path / "out").glob("*.md")).read_text()
    assert export(tmp_path, state)["exported"] == 0


def test_damaged_reexport_keeps_archive_and_retries_repair(tmp_path, transcript):
    state = {}
    original = event("Start") + event("Earlier content " * 40, 1) + event("Last", 2)
    transcript.write_bytes(original)
    export(tmp_path, state)
    archive = next((tmp_path / "out").glob("*.md"))
    contents = archive.read_bytes()
    timestamp = state["claude_code"]["sessions"]["fixture"]["latest_timestamp"]
    transcript.write_bytes(event("Start") + b"{broken\n" + event("Last", 2))
    result = export(tmp_path, state)
    assert result["skipped_shrunk"] == 1
    assert result["exported"] == 0
    assert archive.read_bytes() == contents
    assert state["claude_code"]["sessions"]["fixture"]["latest_timestamp"] == timestamp
    transcript.write_bytes(original)
    assert export(tmp_path, state)["exported"] == 1


def test_unreadable_session_isolated(tmp_path, transcript):
    (transcript.parent / "b.jsonl").write_bytes(event("Healthy", session_id="healthy"))
    real_open = Path.open

    def guarded_open(path, *args, **kwargs):
        if path == transcript:
            raise PermissionError("private information must not be echoed")
        return real_open(path, *args, **kwargs)

    with patch.object(Path, "open", guarded_open):
        result = export(tmp_path, {})
    assert result["failed"] == 1
    assert result["exported"] == 1
    assert str(transcript) in str(result["warnings"])
    assert "private information" not in str(result)


def test_dry_run_corruption_does_not_mutate_state_or_archive(tmp_path, transcript):
    state = {}
    export(tmp_path, state)
    before = copy.deepcopy(state)
    archive = next((tmp_path / "out").glob("*.md"))
    contents = archive.read_bytes()
    transcript.write_bytes(event("Start") + b"broken\n" + event("Last", 2))
    result = export_claude_code(tmp_path / "out", state, full=True, dry_run=True,
                                since_date=None, project_dirs=(transcript.parent,), history_files=())
    assert result["skipped_lines"] == 1
    assert state == before
    assert archive.read_bytes() == contents


def test_cli_reports_counts_path_and_line(tmp_path, transcript, monkeypatch, capsys):
    transcript.write_bytes(event("Start") + b"bad\n" + b"\x00" * 19 + event("Recovered", 1))
    monkeypatch.setattr(sys, "argv", ["export_sessions.py", "--source", "claude-code"])
    monkeypatch.setattr(cli, "run_export", lambda *args, **kwargs: [export(tmp_path, {})])
    cli.main()
    captured = capsys.readouterr()
    assert "skipped_lines=1 recovered_lines=1" in captured.out
    assert f"{transcript}: line 2:" in captured.err
    assert f"{transcript}: line 3:" in captured.err


def test_all_sources_continue_after_corrupt_claude_record(tmp_path, transcript, monkeypatch):
    transcript.write_bytes(event("Start") + b"bad\n")
    for source in ("opencode", "antigravity", "codex", "cursor", "cursor_cli", "dsh", "gemini", "grok", "workbuddy"):
        monkeypatch.setattr(cli, f"export_{source}",
                            lambda *args, name=source, **kwargs: {"source": name, "exported": 0, "scanned": 0})
    state_file = tmp_path / "state.json"
    results = cli.run_export("all", full=False, dry_run=False, base_dir=tmp_path / "archive",
                             state_file=state_file, claude_project_dirs=(transcript.parent,),
                             claude_history_files=(tmp_path / "absent.jsonl",))
    assert results[1]["skipped_lines"] == 1
    assert results[-1]["source"] == "workbuddy"
    assert state_file.is_file()


@pytest.mark.parametrize("bad_fields", [{"timestamp": "invalid"}, {"display": 123}, {"sessionId": []}])
def test_invalid_history_fields_do_not_hide_later_titles(tmp_path, transcript, bad_fields):
    history = tmp_path / "history.jsonl"
    invalid = {"sessionId": "fixture", "display": "Invalid", "timestamp": 1, **bad_fields}
    valid = {"sessionId": "fixture", "display": "Valid history title", "timestamp": 2}
    history.write_text(json.dumps(invalid) + "\n" + json.dumps(valid) + "\n")
    result = export(tmp_path, {}, history_files=(history,))
    assert result["skipped_lines"] == 1
    assert result["warnings"][0]["line"] == 1
    assert next((tmp_path / "out").glob("*.md")).name.endswith("Valid_history_title.md")


def test_fully_damaged_existing_session_repairs_at_same_timestamp(tmp_path, transcript):
    state = {}
    original = event("Start") + event("Original", 1) + event("Last", 2)
    transcript.write_bytes(original)
    export(tmp_path, state)
    transcript.write_bytes(b"bad\n")
    assert export(tmp_path, state)["skipped_lines"] == 1
    transcript.write_bytes(event("Start") + event("Repair", 1) + event("Last", 2))
    result = export(tmp_path, state)
    assert result["exported"] == 1
    assert "Repair" in next((tmp_path / "out").glob("*.md")).read_text()


@pytest.mark.parametrize("dry_run", [False, True])
def test_unencodable_record_keeps_archive_and_continues(tmp_path, transcript, dry_run):
    state = {}
    export(tmp_path, state)
    archive = next((tmp_path / "out").glob("*.md"))
    contents = archive.read_bytes()
    transcript.write_bytes(event("\ud800"))
    (transcript.parent / "b.jsonl").write_bytes(event("Healthy", session_id="healthy"))
    result = export_claude_code(tmp_path / "out", state, full=True, dry_run=dry_run,
                                since_date=None, project_dirs=(transcript.parent,), history_files=())
    assert result["failed"] == 1
    assert result["exported"] == 1
    assert archive.read_bytes() == contents


def test_write_failure_keeps_existing_archive_and_continues(tmp_path, transcript, monkeypatch):
    state = {}
    export(tmp_path, state)
    archive = next((tmp_path / "out").glob("*.md"))
    contents = archive.read_bytes()
    before = copy.deepcopy(state["claude_code"]["sessions"]["fixture"])
    transcript.write_bytes(event("Start") + event("New", 1))
    (transcript.parent / "b.jsonl").write_bytes(event("Healthy", session_id="healthy"))
    real_replace = Path.replace

    def fail_replace(path, target):
        if target == archive:
            raise OSError("synthetic disk error")
        return real_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_replace)
    result = export(tmp_path, state)
    assert result["failed"] == 1
    assert result["exported"] == 1
    assert archive.read_bytes() == contents
    assert state["claude_code"]["sessions"]["fixture"] == before
    assert not list((tmp_path / "out").glob(".claude-export-*"))
