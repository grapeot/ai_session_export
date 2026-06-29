from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_session_export.cli import DEFAULT_OPENCODE_DB, run_export
from ai_session_export.markdown import render_markdown
from ai_session_export.models import MessageTurn, SessionRecord
from ai_session_export.sources.antigravity import (
    DEFAULT_ANTIGRAVITY_BRAIN_DIR,
    export_antigravity,
)
from ai_session_export.sources.claude_code import export_claude_code, parse_claude_session_file
from ai_session_export.sources.opencode import export_opencode
from ai_session_export.sources.second_mind import export_second_mind
from ai_session_export.state import DEFAULT_STATE, load_state, save_state
from ai_session_export.utils import sanitize_filename, should_skip_session, unique_output_path, yaml_string


# --------------------------------------------------------------------------- #
# 1. Unit tests (always run, no external deps)
# --------------------------------------------------------------------------- #


def test_sanitize_filename() -> None:
    assert sanitize_filename("应对新老板推翻路线与文档埋坑") == "应对新老板推翻路线与文档埋坑"
    assert sanitize_filename(" AI  @@@  Session !!! ") == "AI_Session"
    assert sanitize_filename("a__b---c") == "a_b_c"
    assert sanitize_filename("") == "untitled"
    assert len(sanitize_filename("x" * 200)) == 80


def test_should_skip_session() -> None:
    assert should_skip_session("@explore subagent: run task")
    assert should_skip_session("Search anything subagent")
    assert should_skip_session("Find details with subagent now")
    assert not should_skip_session("My normal user session")


def test_render_markdown_second_mind_shape() -> None:
    record = SessionRecord(
        source="second_mind",
        session_id="0f674620-18a8-40a0-8ade-2fae63dc1e5e",
        title="应对新老板推翻路线与文档埋坑",
        date="2025-11-20",
        messages=[
            MessageTurn(role="user", content="我是一个Scientist"),
            MessageTurn(role="assistant", content="这是一份为你准备的"),
        ],
    )
    output = render_markdown(record)
    assert output.startswith("---\nsource: second_mind\n")
    assert 'session_id: "0f674620-18a8-40a0-8ade-2fae63dc1e5e"' in output
    assert 'title: "应对新老板推翻路线与文档埋坑"' in output
    assert 'date: "2025-11-20"' in output
    assert "message_count: 2" in output
    assert "\n# 应对新老板推翻路线与文档埋坑\n" in output
    assert "\n## User\n\n我是一个Scientist\n" in output
    assert "\n## Assistant\n\n这是一份为你准备的\n" in output


def test_render_markdown_opencode_shape() -> None:
    record = SessionRecord(
        source="opencode",
        session_id="ses_377f8237dffe",
        title="AI Era Scaling and Organizational Knowledge Transfer",
        date="2026-02-22",
        project_directory="/home/user/project",
        models_used=["claude-opus-4-6", "claude-haiku-4-5"],
        messages=[
            MessageTurn(role="user", content="Question"),
            MessageTurn(role="assistant", content="Answer"),
        ],
    )
    output = render_markdown(record)
    assert output.startswith("---\nsource: opencode\n")
    assert 'session_id: "ses_377f8237dffe"' in output
    assert 'project_directory: "/home/user/project"' in output
    assert 'models_used: ["claude-opus-4-6", "claude-haiku-4-5"]' in output
    assert "\n## User\n\nQuestion\n" in output
    assert "\n## Assistant\n\nAnswer\n" in output


def test_render_markdown_with_timestamps() -> None:
    # 09:30 and 09:31 local time on a fixed date, expressed as ms epoch.
    t_user = int(datetime(2026, 2, 22, 9, 30).timestamp() * 1000)
    t_assistant = int(datetime(2026, 2, 22, 9, 31).timestamp() * 1000)
    record = SessionRecord(
        source="opencode",
        session_id="ses_ts",
        title="Timestamped session",
        date="2026-02-22",
        messages=[
            MessageTurn(role="user", content="Question", time_created=t_user),
            MessageTurn(role="assistant", content="Answer", time_created=t_assistant),
        ],
    )
    output = render_markdown(record)
    assert "\n## User [09:30]\n\nQuestion\n" in output
    assert "\n## Assistant [09:31]\n\nAnswer\n" in output
    # Frontmatter date is independent of per-turn headers.
    assert 'date: "2026-02-22"' in output


def test_state_load_save_roundtrip(tmp_path: Path) -> None:
    state_file = tmp_path / ".export_state.json"
    assert not state_file.exists()

    loaded = load_state(state_file)
    assert loaded == DEFAULT_STATE

    loaded["opencode"]["last_session_time"] = 123456
    loaded["custom_key"] = "value"
    save_state(loaded, state_file)
    assert state_file.exists()

    reloaded = load_state(state_file)
    assert reloaded["opencode"]["last_session_time"] == 123456
    assert reloaded["custom_key"] == "value"
    # Defaults for other sources are preserved on reload.
    assert reloaded["second_mind"]["last_export_count"] == 0
    assert reloaded["antigravity"]["last_timestamp"] == 0


def test_state_defaults() -> None:
    state = load_state(Path("/nonexistent/ai-session-export-state.json"))
    assert state["second_mind"] == {"last_export_count": 0}
    assert state["opencode"] == {"last_session_time": 0}
    assert state["claude_code"] == {"last_timestamp": 0}
    assert state["antigravity"] == {"last_timestamp": 0}


def test_unique_output_path(tmp_path: Path) -> None:
    first = unique_output_path(tmp_path, "2026-06-29", "My session title")
    assert first.name == "20260629_My_session_title.md"
    first.write_text("v1", encoding="utf-8")

    second = unique_output_path(tmp_path, "2026-06-29", "My session title")
    assert second.name == "20260629_My_session_title_2.md"
    second.write_text("v2", encoding="utf-8")

    third = unique_output_path(tmp_path, "2026-06-29", "My session title")
    assert third.name == "20260629_My_session_title_3.md"


def test_yaml_string() -> None:
    assert yaml_string("simple") == '"simple"'
    assert yaml_string('with "quotes"') == '"with \\"quotes\\""'
    assert yaml_string("中文标题") == '"中文标题"'
    # Embedded YAML-significant chars are JSON-quoted, so they stay safe.
    assert yaml_string("a: b") == '"a: b"'


# --------------------------------------------------------------------------- #
# Fixture builders (synthetic, public-safe data)
# --------------------------------------------------------------------------- #


def _write_second_mind_json(path: Path) -> None:
    path.write_text(
        json.dumps(
            [
                {
                    "conversation_id": "conv-fixture-1",
                    "title": "Fixture Second Mind Chat",
                    "created_at": "2026-06-29 10:00:00.000000",
                    "messages": [
                        {"role": "user", "content": "What is 2 plus 2?"},
                        {"role": "assistant", "content": "The answer is 4."},
                        {"role": "system", "content": "should be dropped"},
                    ],
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _seed_opencode_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE session (
            id TEXT PRIMARY KEY, title TEXT, directory TEXT, time_created INTEGER
        );
        CREATE TABLE message (
            id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT
        );
        CREATE TABLE part (
            id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
            time_created INTEGER, data TEXT
        );
        """
    )
    session_time = int(datetime(2026, 6, 29, 9, 0).timestamp() * 1000)
    conn.execute(
        "INSERT INTO session (id, title, directory, time_created) VALUES (?,?,?,?)",
        ("ses_fixture", "Fixture OpenCode Session", "/home/user/project", session_time),
    )
    turns = [
        ("user", "Tell me about pytest", None, int(datetime(2026, 6, 29, 9, 15).timestamp() * 1000)),
        ("assistant", "pytest is a testing framework", "fixture/model", int(datetime(2026, 6, 29, 9, 16).timestamp() * 1000)),
    ]
    for i, (role, text, model_id, msg_ts) in enumerate(turns):
        msg_id = f"ses_fixture_m{i}"
        data: dict = {"role": role}
        if model_id:
            data["modelID"] = model_id
        conn.execute(
            "INSERT INTO message (id, session_id, time_created, data) VALUES (?,?,?,?)",
            (msg_id, "ses_fixture", msg_ts, json.dumps(data)),
        )
        conn.execute(
            "INSERT INTO part (id, message_id, session_id, time_created, data) VALUES (?,?,?,?,?)",
            (f"{msg_id}_p0", msg_id, "ses_fixture", msg_ts, json.dumps({"type": "text", "text": text})),
        )
    conn.commit()
    conn.close()


def _write_claude_session(projects_root: Path, history_file: Path) -> None:
    project_dir = projects_root / "-home-user-project"
    project_dir.mkdir(parents=True, exist_ok=True)
    history_file.write_text(
        json.dumps(
            {
                "display": "Fixture Claude Task",
                "timestamp": 1711260000000,
                "project": "/home/user/project",
                "sessionId": "claude-fixture-1",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    (project_dir / "claude-fixture-1.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "user",
                        "timestamp": "2026-06-29T09:00:00Z",
                        "sessionId": "claude-fixture-1",
                        "cwd": "/home/user/project",
                        "message": {"role": "user", "content": "Review the fixture code"},
                        "isSidechain": False,
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "timestamp": "2026-06-29T09:05:00Z",
                        "sessionId": "claude-fixture-1",
                        "cwd": "/home/user/project",
                        "message": {
                            "role": "assistant",
                            "model": "claude-opus-4-6",
                            "content": [
                                {"type": "tool_use", "name": "Glob"},
                                {"type": "text", "text": "The fixture looks good."},
                            ],
                        },
                        "isSidechain": False,
                    }
                ),
                json.dumps(
                    {
                        "type": "user",
                        "timestamp": "2026-06-29T09:06:00Z",
                        "sessionId": "claude-fixture-1",
                        "cwd": "/home/user/project",
                        "message": {
                            "role": "user",
                            "content": [{"type": "tool_result", "content": "tool noise"}],
                        },
                        "isSidechain": False,
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _write_antigravity_transcript(brain_dir: Path, session_id: str = "antigravity-session-fixture") -> None:
    transcript_dir = brain_dir / session_id / ".system_generated" / "logs"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    transcript = transcript_dir / "transcript_full.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "step_index": 0,
                        "source": "USER_EXPLICIT",
                        "type": "USER_INPUT",
                        "status": "DONE",
                        "created_at": "2026-06-29T16:38:12Z",
                        "content": "<USER_REQUEST>\nFix the bug in auth.py\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\nActive Document: /home/user/project/auth.py\n</ADDITIONAL_METADATA>",
                    }
                ),
                json.dumps(
                    {
                        "step_index": 1,
                        "source": "SYSTEM",
                        "type": "CONVERSATION_HISTORY",
                        "status": "DONE",
                        "created_at": "2026-06-29T16:38:12Z",
                    }
                ),
                json.dumps(
                    {
                        "step_index": 2,
                        "source": "MODEL",
                        "type": "PLANNER_RESPONSE",
                        "status": "DONE",
                        "created_at": "2026-06-29T16:38:14Z",
                        "content": "I'll look at the auth.py file first.",
                        "tool_calls": [{"name": "view_file", "args": {"path": "/home/user/project/auth.py"}}],
                    }
                ),
                json.dumps(
                    {
                        "step_index": 3,
                        "source": "MODEL",
                        "type": "CODE_ACTION",
                        "status": "DONE",
                        "created_at": "2026-06-29T16:38:15Z",
                        "content": "",
                    }
                ),
                json.dumps(
                    {
                        "step_index": 4,
                        "source": "MODEL",
                        "type": "PLANNER_RESPONSE",
                        "status": "DONE",
                        "created_at": "2026-06-29T16:39:00Z",
                        "content": "The bug is on line 42. The fix is to check for None before accessing the attribute.",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
# 2. Source adapter unit tests (tmp_path + synthetic fixtures)
# --------------------------------------------------------------------------- #


def test_second_mind_export_with_fixture(tmp_path: Path) -> None:
    source_json = tmp_path / "second_mind_export.json"
    _write_second_mind_json(source_json)

    state = load_state(tmp_path / ".state.json")
    result = export_second_mind(
        tmp_path / "second_mind",
        state,
        source_json=source_json,
        full=True,
        dry_run=False,
        since_date=None,
    )
    assert result["total"] == 1
    assert result["exported"] == 1

    files = list((tmp_path / "second_mind").glob("*.md"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert "source: second_mind" in text
    assert "## User" in text
    assert "What is 2 plus 2?" in text
    assert "The answer is 4." in text
    # System-role messages are dropped by the adapter.
    assert "should be dropped" not in text


def test_opencode_export_with_fixture(tmp_path: Path) -> None:
    db_path = tmp_path / "opencode.db"
    _seed_opencode_db(db_path)

    state = {"opencode": {"last_session_time": 0}}
    result = export_opencode(
        tmp_path / "out", state, db_path=db_path, full=True, dry_run=False, since_date=None
    )
    assert result["exported"] == 1

    files = list((tmp_path / "out").glob("*.md"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert "source: opencode" in text
    assert "## User [09:15]" in text
    assert "## Assistant [09:16]" in text
    assert "Tell me about pytest" in text
    assert 'project_directory: "/home/user/project"' in text
    assert "fixture/model" in text  # surfaced in models_used


def test_claude_code_export_with_fixture(tmp_path: Path) -> None:
    projects_root = tmp_path / "projects"
    history_file = tmp_path / "history.jsonl"
    _write_claude_session(projects_root, history_file)

    state = {"claude_code": {"last_timestamp": 0}}
    result = export_claude_code(
        tmp_path / "claude_code",
        state,
        full=False,
        dry_run=False,
        since_date=None,
        project_dirs=(projects_root,),
        history_files=(history_file,),
    )
    assert result["exported"] == 1

    files = list((tmp_path / "claude_code").glob("*.md"))
    assert len(files) == 1
    content = files[0].read_text(encoding="utf-8")
    assert "source: claude_code" in content
    assert "Fixture Claude Task" in content
    assert 'project_directory: "/home/user/project"' in content
    # tool_result user message has no text content and is dropped.
    assert "tool noise" not in content
    # Assistant text survives even though a tool_use item sat next to it.
    assert "The fixture looks good." in content
    assert [m.role for m in parse_claude_session_file(
        projects_root / "-home-user-project" / "claude-fixture-1.jsonl",
        {"claude-fixture-1": [(1711260000000, "Fixture Claude Task")]},
    ).record.messages] == ["user", "assistant"]


def test_antigravity_export_with_fixture(tmp_path: Path) -> None:
    brain_dir = tmp_path / "brain"
    _write_antigravity_transcript(brain_dir)

    state = {"antigravity": {"last_timestamp": 0}}
    result = export_antigravity(
        tmp_path / "antigravity",
        state,
        brain_dir=brain_dir,
        full=True,
        dry_run=False,
        since_date=None,
    )
    assert result["scanned"] == 1
    assert result["exported"] == 1

    files = list((tmp_path / "antigravity").glob("*.md"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")

    # Frontmatter
    assert text.startswith("---\nsource: antigravity\n")
    assert 'session_id: "antigravity-session-fixture"' in text
    assert 'date: "2026-06-29"' in text
    assert "message_count: 3" in text

    # Title is derived from the first user message (inner USER_REQUEST text).
    assert 'title: "Fix the bug in auth.py"' in text
    assert "\n# Fix the bug in auth.py\n" in text

    # XML wrappers stripped: only the inner request text remains.
    assert "<USER_REQUEST>" not in text
    assert "</USER_REQUEST>" not in text
    assert "<ADDITIONAL_METADATA>" not in text
    assert "Active Document:" not in text

    # Only explicit user input + model planner responses survive.
    # The fixture has one USER_INPUT and two PLANNER_RESPONSE steps;
    # CONVERSATION_HISTORY, CODE_ACTION and tool_calls are dropped.
    assert text.count("## User") == 1
    assert text.count("## Assistant") == 2
    assert "I'll look at the auth.py file first." in text
    assert "The bug is on line 42" in text
    assert "view_file" not in text


# --------------------------------------------------------------------------- #
# 3. Integration test (self-contained; also runnable via `pytest -m integration`)
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_cli_run_export_all_sources(tmp_path: Path) -> None:
    second_mind_json = tmp_path / "second_mind_export.json"
    _write_second_mind_json(second_mind_json)

    opencode_db = tmp_path / "opencode.db"
    _seed_opencode_db(opencode_db)

    projects_root = tmp_path / "projects"
    history_file = tmp_path / "history.jsonl"
    _write_claude_session(projects_root, history_file)

    brain_dir = tmp_path / "brain"
    _write_antigravity_transcript(brain_dir)

    state_file = tmp_path / ".export_state.json"
    results = run_export(
        "all",
        full=True,
        dry_run=False,
        base_dir=tmp_path,
        state_file=state_file,
        second_mind_json=second_mind_json,
        opencode_db=opencode_db,
        antigravity_brain_dir=brain_dir,
        claude_project_dirs=(projects_root,),
        claude_history_files=(history_file,),
    )

    assert {r["source"] for r in results} == {"second_mind", "opencode", "claude_code", "antigravity"}

    # Each source produced at least one markdown file under base_dir.
    for sub in ("second_mind", "opencode", "claude_code", "antigravity"):
        assert list((tmp_path / sub).glob("*.md")), f"no markdown emitted for {sub}"

    # State file was persisted with refreshed counters.
    persisted = load_state(state_file)
    assert persisted["second_mind"]["last_export_count"] == 1
    assert persisted["opencode"]["last_session_time"] > 0
    assert persisted["claude_code"]["last_timestamp"] > 0
    assert persisted["antigravity"]["last_timestamp"] > 0


# --------------------------------------------------------------------------- #
# 4. Live end-to-end tests (real data; skipped unless AI_SESSION_EXPORT_LIVE=1)
# --------------------------------------------------------------------------- #


@pytest.mark.live_e2e
class TestLiveExport:
    @pytest.fixture(autouse=True)
    def _check_live(self) -> None:
        if not os.environ.get("AI_SESSION_EXPORT_LIVE"):
            pytest.skip("Set AI_SESSION_EXPORT_LIVE=1 to run live tests")

    def test_live_antigravity_export(self, tmp_path: Path) -> None:
        """Export 7 days of real Antigravity sessions."""
        if not DEFAULT_ANTIGRAVITY_BRAIN_DIR.is_dir():
            pytest.skip(f"Antigravity brain dir not found: {DEFAULT_ANTIGRAVITY_BRAIN_DIR}")

        since = date.today() - timedelta(days=7)

        # Dry-run first: scan only, no files written.
        export_antigravity(
            tmp_path / "dry",
            {},
            brain_dir=DEFAULT_ANTIGRAVITY_BRAIN_DIR,
            full=True,
            dry_run=True,
            since_date=since,
        )

        # Real export.
        result = export_antigravity(
            tmp_path / "antigravity",
            {},
            brain_dir=DEFAULT_ANTIGRAVITY_BRAIN_DIR,
            full=True,
            dry_run=False,
            since_date=since,
        )
        files = list((tmp_path / "antigravity").glob("*.md"))
        if result["exported"] == 0:
            pytest.skip("No recent Antigravity sessions to export")
        assert len(files) == result["exported"]
        sample = files[0].read_text(encoding="utf-8")
        assert "source: antigravity" in sample

    def test_live_opencode_export(self, tmp_path: Path) -> None:
        """Export recent OpenCode sessions."""
        if not DEFAULT_OPENCODE_DB.exists():
            pytest.skip(f"OpenCode DB not found: {DEFAULT_OPENCODE_DB}")

        since = date.today() - timedelta(days=7)

        # Dry-run first.
        export_opencode(
            tmp_path / "dry",
            {},
            db_path=DEFAULT_OPENCODE_DB,
            full=True,
            dry_run=True,
            since_date=since,
        )

        # Real export.
        result = export_opencode(
            tmp_path / "opencode",
            {},
            db_path=DEFAULT_OPENCODE_DB,
            full=True,
            dry_run=False,
            since_date=since,
        )
        files = list((tmp_path / "opencode").glob("*.md"))
        if result["exported"] == 0:
            pytest.skip("No recent OpenCode sessions to export")
        assert len(files) == result["exported"]
        sample = files[0].read_text(encoding="utf-8")
        assert "source: opencode" in sample
