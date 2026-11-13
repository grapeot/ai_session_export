"""WorkBuddy adapter tests on synthetic fixtures shaped like WorkBuddy 5.9 storage."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from ai_session_export.sources.workbuddy import (
    export_workbuddy,
    load_workbuddy_meta,
    parse_workbuddy_session_file,
)

SID_A = "11111111-2222-4333-8444-555555555555"
SID_B = "66666666-7777-4888-8999-aaaaaaaaaaaa"
CWD = "/home/example/WorkBuddy/demo"
T0 = 1794528420000  # ms


def _reminder(query: str) -> str:
    return (
        '<system-reminder data-role="user-context">\n<user_info>\nOS Version: darwin\n'
        f"Workspace Folder: {CWD}\n</user_info>\n<identity_context>secret identity notes</identity_context>\n"
        f"</system-reminder>\n<user_query>{query}</user_query>"
    )


def _provider(model: str = "Example 27B") -> dict:
    return {"model": "org/example-27b", "requestModelName": model, "agent": "cli"}


def _write_session(root: Path, sid: str, lines: list[dict], partial_tail: bool = False) -> Path:
    project = root / CWD.replace("/", "-")
    project.mkdir(parents=True, exist_ok=True)
    path = project / f"{sid}.jsonl"
    text = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in lines)
    if partial_tail:
        text += '{"type": "message", "role": "assis'
    path.write_text(text, encoding="utf-8")
    # spill directory for tool results must not be mistaken for a transcript
    (project / sid / "tool-results").mkdir(parents=True, exist_ok=True)
    (project / sid / "tool-results" / "x.jsonl").write_text("{}\n", encoding="utf-8")
    return path


def _session_a() -> list[dict]:
    return [
        {"type": "session-meta", "sessionId": SID_A, "timestamp": T0, "meta": {}},
        {"type": "message", "role": "user", "sessionId": SID_A, "cwd": CWD, "timestamp": T0 + 1000,
         "content": [{"type": "input_text", "text": _reminder("Draft a reminder to Alex for Thursday 3 PM.")}]},
        {"type": "ai-title", "aiTitle": "Draft meeting reminder", "sessionId": SID_A, "cwd": CWD, "timestamp": T0 + 1500},
        {"type": "reasoning", "sessionId": SID_A, "timestamp": T0 + 2000, "providerData": _provider(),
         "content": [], "rawContent": [{"type": "reasoning_text", "text": "private chain of thought"}]},
        {"type": "message", "role": "assistant", "sessionId": SID_A, "cwd": CWD, "timestamp": T0 + 3000,
         "providerData": _provider(), "content": [{"type": "output_text", "text": "Here is a draft for Thursday."}]},
        {"type": "message", "role": "user", "sessionId": SID_A, "cwd": CWD, "timestamp": T0 + 60000,
         "content": [{"type": "input_text", "text": _reminder("No, move it to Friday 10 AM and save it.")}]},
        {"type": "function_call", "sessionId": SID_A, "timestamp": T0 + 61000, "providerData": _provider(),
         "name": "Write", "callId": "call_1", "arguments": "{\"file_path\": \"draft.md\"}"},
        {"type": "function_call_result", "sessionId": SID_A, "timestamp": T0 + 61100, "name": "Write",
         "callId": "call_1", "status": "completed", "output": {"type": "text", "text": "wrote draft.md"}},
        {"type": "message", "role": "assistant", "sessionId": SID_A, "cwd": CWD, "timestamp": T0 + 62000,
         "providerData": _provider(), "content": [{"type": "output_text", "text": "Moved to Friday 10 AM and saved."}]},
    ]


def _session_b() -> list[dict]:
    return [
        {"type": "message", "role": "user", "sessionId": SID_B, "cwd": CWD, "timestamp": T0 + 120000,
         "content": [{"type": "input_text", "text": _reminder("Explain progressive disclosure in three sentences.")}]},
        {"type": "message", "role": "assistant", "sessionId": SID_B, "cwd": CWD, "timestamp": T0 + 125000,
         "providerData": _provider("Other Model"), "content": [{"type": "output_text", "text": "Show the core first."}]},
    ]


def _make_db(path: Path, rows: list[tuple[str, str, str, str]], deleted: dict | None = None) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT, title TEXT, custom_title TEXT, status TEXT, "
                 "deleted_at INTEGER)")
    conn.executemany("INSERT INTO sessions (id, cwd, title, custom_title, status, deleted_at) VALUES (?, ?, ?, ?, ?, ?)",
                     [(sid, CWD, title, custom, status, (deleted or {}).get(sid)) for sid, title, custom, status in rows])
    conn.commit()
    conn.close()


def test_parse_keeps_only_user_query_and_assistant_text(tmp_path: Path) -> None:
    path = _write_session(tmp_path, SID_A, _session_a(), partial_tail=True)
    parsed = parse_workbuddy_session_file(path, {})
    assert parsed is not None
    record = parsed.record
    assert record.source == "workbuddy"
    assert record.session_id == SID_A
    assert record.title == "Draft meeting reminder"  # ai-title when the DB has nothing
    assert record.project_directory == CWD
    assert [m.role for m in record.messages] == ["user", "assistant", "user", "assistant"]
    assert record.messages[0].content == "Draft a reminder to Alex for Thursday 3 PM."
    joined = "\n".join(m.content for m in record.messages)
    for private in ("system-reminder", "secret identity notes", "private chain of thought", "wrote draft.md"):
        assert private not in joined
    assert record.models_used == ["org/example-27b"]  # provider model id, not the UI label
    assert record.messages[0].model == "org/example-27b"
    assert parsed.latest_timestamp_ms == T0 + 62000


def test_user_text_without_query_tag_strips_reminder(tmp_path: Path) -> None:
    lines = [{"type": "message", "role": "user", "sessionId": SID_B, "timestamp": T0,
              "content": [{"type": "input_text", "text": "<system-reminder>ctx</system-reminder>\nplain request"}]},
             {"type": "message", "role": "assistant", "sessionId": SID_B, "timestamp": T0 + 1,
              "content": [{"type": "output_text", "text": "ok"}]}]
    parsed = parse_workbuddy_session_file(_write_session(tmp_path, SID_B, lines), {})
    assert parsed is not None and parsed.record.messages[0].content == "plain request"


def test_meta_prefers_custom_title_exports_archived_skips_deleted(tmp_path: Path) -> None:
    db = tmp_path / "workbuddy.db"
    sid_c = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
    _make_db(db, [(SID_A, "Draft meeting reminder", "Renamed by user", "completed"),
                  (SID_B, "Explain progressive disclosure", "", "archived"),
                  (sid_c, "Gone", "", "completed")], deleted={sid_c: 1794528999000})
    meta = load_workbuddy_meta(db)
    a = parse_workbuddy_session_file(_write_session(tmp_path / "p", SID_A, _session_a()), meta)
    b = parse_workbuddy_session_file(_write_session(tmp_path / "p", SID_B, _session_b()), meta)
    c_lines = [dict(item, sessionId=sid_c) for item in _session_b()]
    c = parse_workbuddy_session_file(_write_session(tmp_path / "p", sid_c, c_lines), meta)
    assert a is not None and a.record.title == "Renamed by user"
    assert b is not None and b.record.title == "Explain progressive disclosure" and b.record.surface == ""
    assert c is None  # deleted in the app


def test_missing_or_relative_db_is_tolerated(tmp_path: Path, monkeypatch) -> None:
    assert load_workbuddy_meta(tmp_path / "nope.db") is None
    db = tmp_path / "workbuddy.db"
    _make_db(db, [(SID_A, "T", "", "completed")])
    monkeypatch.chdir(tmp_path)
    assert load_workbuddy_meta(Path("workbuddy.db")) == {SID_A: load_workbuddy_meta(db)[SID_A]}


def test_query_inside_reminder_and_unclosed_reminder_do_not_leak(tmp_path: Path) -> None:
    quoted = ('<system-reminder><user_query>old quoted request</user_query></system-reminder>\n'
              '<user_query>real request</user_query>')
    lines = [{"type": "message", "role": "user", "sessionId": SID_B, "timestamp": T0,
              "content": [{"type": "input_text", "text": quoted}]},
             {"type": "message", "role": "user", "sessionId": SID_B, "timestamp": T0 + 1,
              "content": [{"type": "input_text", "text": "<system-reminder>identity secrets, never closed"}]},
             {"type": "message", "role": "assistant", "sessionId": SID_B, "timestamp": T0 + 2,
              "content": [{"type": "output_text", "text": "ok"}]}]
    parsed = parse_workbuddy_session_file(_write_session(tmp_path, SID_B, lines), {})
    assert parsed is not None
    assert [m.content for m in parsed.record.messages] == ["real request", "ok"]


def test_truncated_utf8_tail_is_skipped(tmp_path: Path) -> None:
    path = _write_session(tmp_path, SID_B, _session_b())
    with path.open("ab") as handle:
        handle.write('{"type": "message", "role": "user", "content": "中'.encode("utf-8")[:-1])
    parsed = parse_workbuddy_session_file(path, {})
    assert parsed is not None and len(parsed.record.messages) == 2


def test_same_session_in_two_roots_is_read_once_from_newest(tmp_path: Path) -> None:
    old_root, new_root = tmp_path / "old", tmp_path / "new"
    old = _write_session(old_root, SID_A, _session_a()[:5])
    new = _write_session(new_root, SID_A, _session_a())
    os.utime(old, (1, 1))
    out = tmp_path / "out"
    state: dict = {}
    result = export_workbuddy(out, state, full=False, dry_run=False, since_date=None,
                              project_dirs=(new_root, old_root), db_path=tmp_path / "none.db")
    assert result == {"source": "workbuddy", "scanned": 1, "exported": 1}
    text = (out / state["workbuddy"]["sessions"][SID_A]["output_file"]).read_text(encoding="utf-8")
    assert "Moved to Friday 10 AM and saved." in text
    assert new.exists()


def test_export_incremental_and_rename(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    _write_session(root, SID_A, _session_a())
    _write_session(root, SID_B, _session_b())
    db = tmp_path / "workbuddy.db"
    _make_db(db, [(SID_A, "Draft meeting reminder", "", "completed")])
    out = tmp_path / "out"
    state: dict = {}

    first = export_workbuddy(out, state, full=False, dry_run=False, since_date=None, project_dirs=(root,), db_path=db)
    assert first == {"source": "workbuddy", "scanned": 2, "exported": 2}
    files = sorted(p.name for p in out.glob("*.md"))
    assert len(files) == 2
    text = (out / state["workbuddy"]["sessions"][SID_A]["output_file"]).read_text(encoding="utf-8")
    assert "source: workbuddy" in text and "Moved to Friday 10 AM and saved." in text

    again = export_workbuddy(out, state, full=False, dry_run=False, since_date=None, project_dirs=(root,), db_path=db)
    assert again["exported"] == 0

    # Renaming in the app only touches the DB; the export must pick it up.
    conn = sqlite3.connect(db)
    conn.execute("UPDATE sessions SET custom_title = ? WHERE id = ?", ("Friday sync note", SID_A))
    conn.commit()
    conn.close()
    renamed = export_workbuddy(out, state, full=False, dry_run=False, since_date=None, project_dirs=(root,), db_path=db)
    assert renamed["exported"] == 1
    out_a = out / state["workbuddy"]["sessions"][SID_A]["output_file"]
    assert 'title: "Friday sync note"' in out_a.read_text(encoding="utf-8")
    assert len(list(out.glob("*.md"))) == 2  # updated in place, no duplicate

    # The DB becomes unreadable: nothing is re-exported, and the renamed title survives a transcript change.
    db.unlink()
    assert export_workbuddy(out, state, full=False, dry_run=False, since_date=None, project_dirs=(root,),
                            db_path=db)["exported"] == 0
    path_a = next(root.glob(f"*/{SID_A}.jsonl"))
    with path_a.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "message", "role": "assistant", "sessionId": SID_A, "timestamp": T0 + 90000,
                                 "content": [{"type": "output_text", "text": "One more thing."}]}) + "\n")
    assert export_workbuddy(out, state, full=False, dry_run=False, since_date=None, project_dirs=(root,),
                            db_path=db)["exported"] == 1
    text_a = out_a.read_text(encoding="utf-8")
    assert 'title: "Friday sync note"' in text_a and "One more thing." in text_a


def test_delete_in_app_after_export_stops_updates(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    _write_session(root, SID_B, _session_b())
    db = tmp_path / "workbuddy.db"
    _make_db(db, [(SID_B, "Explain", "", "completed")])
    out, state = tmp_path / "out", {}
    assert export_workbuddy(out, state, full=False, dry_run=False, since_date=None, project_dirs=(root,),
                            db_path=db)["exported"] == 1
    conn = sqlite3.connect(db)
    conn.execute("UPDATE sessions SET deleted_at = 1 WHERE id = ?", (SID_B,))
    conn.commit()
    conn.close()
    assert export_workbuddy(out, state, full=False, dry_run=False, since_date=None, project_dirs=(root,),
                            db_path=db)["exported"] == 0


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    _write_session(root, SID_B, _session_b())
    out = tmp_path / "out"
    result = export_workbuddy(out, {}, full=False, dry_run=True, since_date=None, project_dirs=(root,),
                              db_path=tmp_path / "none.db")
    assert result["exported"] == 1
    assert not out.exists() or not any(out.iterdir())


def test_since_date_filters(tmp_path: Path) -> None:
    from datetime import date

    root = tmp_path / "projects"
    _write_session(root, SID_B, _session_b())
    result = export_workbuddy(tmp_path / "out", {}, full=False, dry_run=False, since_date=date(2099, 1, 1),
                              project_dirs=(root,), db_path=tmp_path / "none.db")
    assert result["exported"] == 0
