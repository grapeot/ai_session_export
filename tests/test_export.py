from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import ai_session_export.cli as cli_module
from ai_session_export.cli import DEFAULT_OPENCODE_DB, run_export
from ai_session_export.markdown import render_markdown
from ai_session_export.models import MessageTurn, SessionRecord
from ai_session_export.sources.antigravity import (
    DEFAULT_ANTIGRAVITY_BRAIN_DIR,
    DEFAULT_ANTIGRAVITY_BRAIN_DIRS,
    export_antigravity,
)
from ai_session_export.sources.claude_code import export_claude_code, parse_claude_session_file
from ai_session_export.sources.codex import (
    DEFAULT_CODEX_SESSION_DIRS,
    DEFAULT_CODEX_SESSION_INDEX,
    export_codex,
    parse_codex_session_file,
)
from ai_session_export.sources.cursor import DEFAULT_CURSOR_DB, export_cursor
from ai_session_export.sources.dsh import DEFAULT_DSH_SESSIONS_DIR, export_dsh, parse_dsh_session_file
from ai_session_export.sources.opencode import export_opencode
from ai_session_export.sources.second_mind import export_second_mind
from ai_session_export.state import DEFAULT_STATE, load_state, save_state
from ai_session_export.utils import ms_to_date, sanitize_filename, should_skip_session, unique_output_path, yaml_string


# --------------------------------------------------------------------------- #
# 1. Unit tests (always run, no external deps)
# --------------------------------------------------------------------------- #


def test_sanitize_filename() -> None:
    assert sanitize_filename("合成测试会话") == "合成测试会话"
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
        session_id="fixture-second-mind-session",
        title="Synthetic Planning Session",
        date="2025-11-20",
        messages=[
            MessageTurn(role="user", content="Synthetic question"),
            MessageTurn(role="assistant", content="Synthetic answer"),
        ],
    )
    output = render_markdown(record)
    assert output.startswith("---\nsource: second_mind\n")
    assert 'session_id: "fixture-second-mind-session"' in output
    assert 'title: "Synthetic Planning Session"' in output
    assert 'date: "2025-11-20"' in output
    assert "message_count: 2" in output
    assert "\n# Synthetic Planning Session\n" in output
    assert "\n## User\n\nSynthetic question\n" in output
    assert "\n## Assistant\n\nSynthetic answer\n" in output
    assert "turn_models:" not in output


def test_render_markdown_opencode_shape() -> None:
    record = SessionRecord(
        source="opencode",
        session_id="fixture-opencode-session",
        title="Synthetic Scaling Session",
        date="2026-02-22",
        project_directory="/home/user/project",
        models_used=["claude-opus-4-6", "claude-haiku-4-5"],
        messages=[
            MessageTurn(role="user", content="Question", model="claude-opus-4-6"),
            MessageTurn(role="assistant", content="Answer", model="claude-opus-4-6"),
        ],
    )
    output = render_markdown(record)
    assert output.startswith("---\nsource: opencode\n")
    assert 'session_id: "fixture-opencode-session"' in output
    assert 'project_directory: "/home/user/project"' in output
    assert 'models_used: ["claude-opus-4-6", "claude-haiku-4-5"]' in output
    assert 'turn_models: ["claude-opus-4-6", "claude-opus-4-6"]' in output
    assert "\n## User\n\nQuestion\n" in output
    assert "\n## Assistant\n\nAnswer\n" in output


def test_render_markdown_turn_models_preserves_null_alignment() -> None:
    record = SessionRecord(
        source="opencode",
        session_id="ses_mixed_models",
        title="Mixed model fixture",
        date="2026-02-22",
        messages=[
            MessageTurn(role="user", content="Question", model="gpt-example"),
            MessageTurn(role="assistant", content="Answer"),
        ],
    )

    output = render_markdown(record)

    assert 'turn_models: ["gpt-example", null]' in output


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
    assert reloaded["antigravity"] == {
        "last_timestamp": 0,
        "legacy_cursor_migrated": False,
        "surfaces": {},
    }


def test_state_loading_legacy_shape_does_not_mutate_defaults(tmp_path: Path) -> None:
    state_file = tmp_path / ".export_state.json"
    state_file.write_text(json.dumps({"antigravity": {"last_timestamp": 123}}), encoding="utf-8")

    loaded = load_state(state_file)
    loaded["antigravity"]["surfaces"]["ide"] = {"sessions": {}}

    assert DEFAULT_STATE["antigravity"]["surfaces"] == {}


def test_state_defaults() -> None:
    state = load_state(Path("/nonexistent/ai-session-export-state.json"))
    assert state["second_mind"] == {"last_export_count": 0}
    assert state["opencode"] == {"last_session_time": 0}
    assert state["claude_code"] == {"last_timestamp": 0}
    assert state["antigravity"] == {
        "last_timestamp": 0,
        "legacy_cursor_migrated": False,
        "surfaces": {},
    }
    assert state["codex"] == {"sessions": {}}


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


def _seed_cursor_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE composerHeaders (
            composerId TEXT PRIMARY KEY, workspaceId TEXT,
            createdAt INTEGER, lastUpdatedAt INTEGER, checkpointAt INTEGER,
            isArchived INTEGER, isSubagent INTEGER, recency INTEGER, value TEXT
        );
        CREATE TABLE cursorDiskKV (key TEXT UNIQUE, value BLOB);
        """
    )
    composer_id = "a6f723dc-9c5b-4169-b03f-31abb1e6069b"
    created_ms = int(datetime(2026, 6, 29, 9, 0).timestamp() * 1000)
    header = {
        "type": "head",
        "composerId": composer_id,
        "name": "Fixture Cursor Session",
        "createdAt": created_ms,
        "lastUpdatedAt": created_ms + 60_000,
        "unifiedMode": "agent",
        "workspaceIdentifier": {
            "id": "54b624f649aa664190b9cdd8e92d92d1",
            "uri": {"fsPath": "/home/user/project", "scheme": "file"},
        },
    }
    conn.execute(
        "INSERT INTO composerHeaders (composerId, createdAt, lastUpdatedAt, isSubagent, value) "
        "VALUES (?,?,?,?,?)",
        (composer_id, created_ms, created_ms + 60_000, 0, json.dumps(header)),
    )
    bubbles = [
        {
            "type": 1,
            "text": "Review the fixture code",
            "modelInfo": {"modelName": "fixture-cursor-model"},
            "createdAt": "2026-06-29T09:15:00.000Z",
        },
        {
            "type": 2,
            "text": "The fixture looks good.",
            "createdAt": "2026-06-29T09:15:01.000Z",
        },
    ]
    for i, bubble in enumerate(bubbles):
        conn.execute(
            "INSERT INTO cursorDiskKV (key, value) VALUES (?,?)",
            (f"bubbleId:{composer_id}:{i:08d}-0000-0000-0000-000000000000", json.dumps(bubble)),
        )
    conn.commit()
    conn.close()


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
        ("user", "Tell me about pytest", "fixture/model", int(datetime(2026, 6, 29, 9, 15).timestamp() * 1000)),
        ("assistant", "pytest is a testing framework", "fixture/model", int(datetime(2026, 6, 29, 9, 16).timestamp() * 1000)),
    ]
    for i, (role, text, model_id, msg_ts) in enumerate(turns):
        msg_id = f"ses_fixture_m{i}"
        data: dict = {"role": role}
        if model_id:
            data["model"] = {"providerID": "fixture", "modelID": model_id} if role == "user" else None
            if role == "assistant":
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
                json.dumps(
                    {
                        "type": "user",
                        "timestamp": "2026-06-29T09:07:00Z",
                        "sessionId": "claude-fixture-1",
                        "cwd": "/home/user/project",
                        "message": {"role": "user", "content": "Check one more fixture"},
                        "isSidechain": False,
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "timestamp": "2026-06-29T09:08:00Z",
                        "sessionId": "claude-fixture-1",
                        "cwd": "/home/user/project",
                        "message": {
                            "role": "assistant",
                            "model": "claude-sonnet-4-6",
                            "content": "The second fixture also looks good.",
                        },
                        "isSidechain": False,
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _write_antigravity_transcript(
    brain_dir: Path,
    session_id: str = "antigravity-session-fixture",
    user_text: str = "Fix the bug in auth.py",
) -> Path:
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
                        "content": f"<USER_REQUEST>\n{user_text}\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\nActive Document: /home/example/project/auth.py\n</ADDITIONAL_METADATA>",
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
    return transcript


def _write_codex_session(session_dir: Path, index_file: Path, *, include_followup: bool = False) -> Path:
    session_dir.mkdir(parents=True, exist_ok=True)
    index_file.write_text(
        json.dumps(
            {
                "id": "codex-fixture-1",
                "thread_name": "Fixture Codex Task",
                "updated_at": "2026-06-29T09:05:00Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    events = [
        {
            "timestamp": "2026-06-29T09:00:00Z",
            "type": "session_meta",
            "payload": {
                "id": "codex-fixture-1",
                "cwd": "/home/user/project",
                "base_instructions": "private system instructions",
            },
        },
        {
            "timestamp": "2026-06-29T09:00:01Z",
            "type": "turn_context",
            "payload": {"cwd": "/home/user/project", "model": "fixture-codex-model"},
        },
        {
            "timestamp": "2026-06-29T09:00:02Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "Review the fixture project"},
        },
        {
            "timestamp": "2026-06-29T09:00:03Z",
            "type": "event_msg",
            "payload": {"type": "agent_reasoning", "text": "private reasoning"},
        },
        {
            "timestamp": "2026-06-29T09:00:04Z",
            "type": "response_item",
            "payload": {"type": "function_call_output", "output": "private tool output"},
        },
        {
            "timestamp": "2026-06-29T09:00:05Z",
            "type": "event_msg",
            "payload": {"type": "agent_message", "phase": "final_answer", "message": "The fixture looks good."},
        },
    ]
    if include_followup:
        events.extend(
            [
                {
                    "timestamp": "2026-06-29T09:04:59Z",
                    "type": "turn_context",
                    "payload": {"model": "fixture-codex-model-2"},
                },
                {
                    "timestamp": "2026-06-29T09:05:00Z",
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "Anything else?"},
                },
                {
                    "timestamp": "2026-06-29T09:05:01Z",
                    "type": "event_msg",
                    "payload": {"type": "agent_message", "phase": "final_answer", "message": "No further issues."},
                },
            ]
        )
    session_file = session_dir / "rollout-2026-06-29T09-00-00-codex-fixture-1.jsonl"
    session_file.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
    return session_file


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
    assert 'turn_models: ["fixture/model", "fixture/model"]' in text


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
    parsed = parse_claude_session_file(
        projects_root / "-home-user-project" / "claude-fixture-1.jsonl",
        {"claude-fixture-1": [(1711260000000, "Fixture Claude Task")]},
    )
    assert parsed is not None
    assert [m.role for m in parsed.record.messages] == ["user", "assistant", "user", "assistant"]
    assert [m.model for m in parsed.record.messages] == [
        "claude-opus-4-6",
        "claude-opus-4-6",
        "claude-sonnet-4-6",
        "claude-sonnet-4-6",
    ]
    assert (
        'turn_models: ["claude-opus-4-6", "claude-opus-4-6", "claude-sonnet-4-6", '
        '"claude-sonnet-4-6"]' in content
    )


def test_claude_missing_assistant_model_does_not_leak_later_model(tmp_path: Path) -> None:
    session_file = tmp_path / "claude-missing-model.jsonl"
    events = [
        {
            "type": "user",
            "timestamp": "2026-06-29T09:00:00Z",
            "sessionId": "claude-missing-model",
            "message": {"role": "user", "content": "First fixture question"},
        },
        {
            "type": "assistant",
            "timestamp": "2026-06-29T09:01:00Z",
            "sessionId": "claude-missing-model",
            "message": {"role": "assistant", "content": "First fixture answer"},
        },
        {
            "type": "user",
            "timestamp": "2026-06-29T09:02:00Z",
            "sessionId": "claude-missing-model",
            "message": {"role": "user", "content": "Second fixture question"},
        },
        {
            "type": "assistant",
            "timestamp": "2026-06-29T09:03:00Z",
            "sessionId": "claude-missing-model",
            "message": {
                "role": "assistant",
                "model": "claude-fixture-later",
                "content": "Second fixture answer",
            },
        },
    ]
    session_file.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

    parsed = parse_claude_session_file(session_file, {})

    assert parsed is not None
    assert [message.model for message in parsed.record.messages] == [
        None,
        None,
        "claude-fixture-later",
        "claude-fixture-later",
    ]


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
    assert 'surface: "ide"' in text
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


def test_antigravity_exports_all_surfaces_incrementally(tmp_path: Path) -> None:
    brain_dirs = {
        "2": tmp_path / "antigravity_2_brain",
        "ide": tmp_path / "antigravity_ide_brain",
        "cli": tmp_path / "antigravity_cli_brain",
    }
    _write_antigravity_transcript(brain_dirs["2"], "shared-session", "Review the desktop fixture")
    _write_antigravity_transcript(brain_dirs["ide"], "shared-session", "Review the IDE fixture")
    cli_transcript = _write_antigravity_transcript(
        brain_dirs["cli"], "shared-session", "Review the CLI fixture"
    )
    state: dict[str, object] = {"antigravity": {"last_timestamp": 0, "surfaces": {}}}
    output_dir = tmp_path / "antigravity"

    first = export_antigravity(
        output_dir,
        state,
        brain_dirs=brain_dirs,
        full=False,
        dry_run=False,
        since_date=None,
    )

    assert first["scanned"] == 3
    assert first["exported"] == 3
    assert first["failed"] == 0
    assert first["surfaces"] == {
        "2": {"scanned": 1, "exported": 1, "failed": 0},
        "ide": {"scanned": 1, "exported": 1, "failed": 0},
        "cli": {"scanned": 1, "exported": 1, "failed": 0},
    }
    files = list(output_dir.glob("*.md"))
    assert len(files) == 3
    rendered = [file.read_text(encoding="utf-8") for file in files]
    assert {line for text in rendered for line in text.splitlines() if line.startswith("surface:")} == {
        'surface: "2"',
        'surface: "ide"',
        'surface: "cli"',
    }

    unchanged = export_antigravity(
        output_dir,
        state,
        brain_dirs=brain_dirs,
        full=False,
        dry_run=False,
        since_date=None,
    )
    assert unchanged["exported"] == 0

    with cli_transcript.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "step_index": 5,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "created_at": "2026-06-29T16:40:00Z",
                    "content": "The CLI follow-up is complete.",
                }
            )
            + "\n"
        )

    updated = export_antigravity(
        output_dir,
        state,
        brain_dirs=brain_dirs,
        full=False,
        dry_run=False,
        since_date=None,
    )
    assert updated["exported"] == 1
    assert updated["surfaces"]["cli"]["exported"] == 1
    assert len(list(output_dir.glob("*.md"))) == 3
    assert any("The CLI follow-up is complete." in file.read_text(encoding="utf-8") for file in files)

    surfaces = state["antigravity"]["surfaces"]
    assert set(surfaces) == {"2", "ide", "cli"}
    assert surfaces["2"]["sessions"]["shared-session"]["status"] == "complete"
    assert surfaces["ide"]["sessions"]["shared-session"]["status"] == "complete"
    assert surfaces["cli"]["sessions"]["shared-session"]["status"] == "complete"


def test_antigravity_malformed_session_does_not_block_other_surfaces(tmp_path: Path) -> None:
    brain_dirs = {"2": tmp_path / "antigravity_2_brain", "ide": tmp_path / "antigravity_ide_brain"}
    malformed = _write_antigravity_transcript(brain_dirs["2"], "malformed-session")
    malformed.write_text("not-json\n" + malformed.read_text(encoding="utf-8"), encoding="utf-8")
    _write_antigravity_transcript(brain_dirs["ide"], "valid-session")
    state: dict[str, object] = {"antigravity": {"last_timestamp": 0, "surfaces": {}}}

    result = export_antigravity(
        tmp_path / "output",
        state,
        brain_dirs=brain_dirs,
        full=False,
        dry_run=False,
        since_date=None,
    )

    assert result["scanned"] == 2
    assert result["exported"] == 1
    assert result["failed"] == 1
    assert result["warnings"] == [
        {
            "surface": "2",
            "line": 1,
            "error": "Expecting value",
        }
    ]
    failed_state = state["antigravity"]["surfaces"]["2"]["sessions"]["malformed-session"]
    assert failed_state["status"] == "failed"
    assert failed_state["error_line"] == 1
    assert state["antigravity"]["surfaces"]["ide"]["sessions"]["valid-session"]["status"] == "complete"

    malformed.write_text("\n".join(malformed.read_text(encoding="utf-8").splitlines()[1:]) + "\n", encoding="utf-8")
    repaired = export_antigravity(
        tmp_path / "output",
        state,
        brain_dirs=brain_dirs,
        full=False,
        dry_run=False,
        since_date=None,
    )
    assert repaired["exported"] == 1
    assert repaired["failed"] == 0
    assert state["antigravity"]["surfaces"]["2"]["sessions"]["malformed-session"]["status"] == "complete"
    assert len(list((tmp_path / "output").glob("*.md"))) == 2


def test_antigravity_valid_json_with_wrong_shape_is_isolated(tmp_path: Path) -> None:
    brain_dirs = {"2": tmp_path / "antigravity_2_brain", "cli": tmp_path / "antigravity_cli_brain"}
    wrong_shape = _write_antigravity_transcript(brain_dirs["2"], "wrong-shape-session")
    wrong_shape.write_text("[]\n" + wrong_shape.read_text(encoding="utf-8"), encoding="utf-8")
    _write_antigravity_transcript(brain_dirs["cli"], "valid-cli-session")

    result = export_antigravity(
        tmp_path / "output",
        {},
        brain_dirs=brain_dirs,
        full=False,
        dry_run=False,
        since_date=None,
    )

    assert result["exported"] == 1
    assert result["failed"] == 1
    assert result["warnings"][0]["error"] == "Expected a JSON object"


def test_antigravity_invalid_field_type_is_isolated_and_state_is_saved(tmp_path: Path) -> None:
    brain_dirs = {"2": tmp_path / "antigravity_2_brain", "cli": tmp_path / "antigravity_cli_brain"}
    invalid = _write_antigravity_transcript(brain_dirs["2"], "invalid-field-session")
    steps = [json.loads(line) for line in invalid.read_text(encoding="utf-8").splitlines()]
    steps[0]["content"] = ["not", "a", "string"]
    invalid.write_text("\n".join(json.dumps(step) for step in steps) + "\n", encoding="utf-8")
    _write_antigravity_transcript(brain_dirs["cli"], "valid-cli-session")
    state_file = tmp_path / "state.json"

    results = run_export(
        "antigravity",
        full=False,
        dry_run=False,
        base_dir=tmp_path / "output",
        state_file=state_file,
        antigravity_brain_dirs=brain_dirs,
    )

    assert results[0]["exported"] == 1
    assert results[0]["failed"] == 1
    assert results[0]["warnings"] == [
        {"surface": "2", "line": 1, "error": "Field 'content' must be a string"}
    ]
    persisted = load_state(state_file)
    assert persisted["antigravity"]["surfaces"]["2"]["sessions"]["invalid-field-session"]["status"] == "failed"
    assert persisted["antigravity"]["surfaces"]["cli"]["sessions"]["valid-cli-session"]["status"] == "complete"


def test_antigravity_legacy_cursor_applies_only_to_ide(tmp_path: Path) -> None:
    brain_dirs = {"2": tmp_path / "antigravity_2_brain", "ide": tmp_path / "antigravity_ide_brain"}
    _write_antigravity_transcript(brain_dirs["2"], "new-surface-session")
    _write_antigravity_transcript(brain_dirs["ide"], "legacy-ide-session")
    state: dict[str, object] = {"antigravity": {"last_timestamp": 9_999_999_999_999}}

    result = export_antigravity(
        tmp_path / "output",
        state,
        brain_dirs=brain_dirs,
        full=False,
        dry_run=False,
        since_date=None,
    )

    assert result["exported"] == 1
    assert result["surfaces"]["2"]["exported"] == 1
    assert result["surfaces"]["ide"]["exported"] == 0
    ide_state = state["antigravity"]["surfaces"]["ide"]["sessions"]["legacy-ide-session"]
    assert ide_state["status"] == "legacy_imported"
    assert state["antigravity"]["legacy_cursor_migrated"] is True


def test_antigravity_legacy_cursor_migration_waits_for_unfiltered_run(tmp_path: Path) -> None:
    brain_dir = tmp_path / "brain"
    _write_antigravity_transcript(brain_dir, "legacy-ide-session")
    state: dict[str, object] = {"antigravity": {"last_timestamp": 9_999_999_999_999}}

    filtered = export_antigravity(
        tmp_path / "output",
        state,
        brain_dir=brain_dir,
        full=False,
        dry_run=False,
        since_date=date(2026, 7, 1),
    )
    assert filtered["exported"] == 0
    assert state["antigravity"].get("legacy_cursor_migrated") is not True

    unfiltered = export_antigravity(
        tmp_path / "output",
        state,
        brain_dir=brain_dir,
        full=False,
        dry_run=False,
        since_date=None,
    )
    assert unfiltered["exported"] == 0
    assert state["antigravity"]["legacy_cursor_migrated"] is True
    assert state["antigravity"]["surfaces"]["ide"]["sessions"]["legacy-ide-session"]["status"] == (
        "legacy_imported"
    )


def test_antigravity_dry_run_does_not_mutate_state(tmp_path: Path) -> None:
    brain_dir = tmp_path / "brain"
    _write_antigravity_transcript(brain_dir)
    state: dict[str, object] = {
        "antigravity": {
            "last_timestamp": 0,
            "legacy_cursor_migrated": False,
            "surfaces": {"cli": {"sessions": {"sentinel": {"status": "ignored"}}}},
        }
    }
    original_state = json.loads(json.dumps(state))

    result = export_antigravity(
        tmp_path / "output",
        state,
        brain_dir=brain_dir,
        full=False,
        dry_run=True,
        since_date=None,
    )

    assert result["exported"] == 1
    assert state == original_state
    assert not (tmp_path / "output").exists()


def test_codex_export_with_fixture_and_incremental_update(tmp_path: Path) -> None:
    session_dir = tmp_path / "sessions"
    index_file = tmp_path / "session_index.jsonl"
    session_file = _write_codex_session(session_dir, index_file)
    state = {"codex": {"sessions": {}}}
    output_dir = tmp_path / "codex"

    first = export_codex(
        output_dir,
        state,
        full=False,
        dry_run=False,
        since_date=None,
        session_dirs=(session_dir,),
        session_index=index_file,
    )
    assert first == {"source": "codex", "scanned": 1, "exported": 1}
    files = list(output_dir.glob("*.md"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert "source: codex" in text
    assert "Fixture Codex Task" in text
    assert "Review the fixture project" in text
    assert "The fixture looks good." in text
    assert "private reasoning" not in text
    assert "private tool output" not in text
    assert "private system instructions" not in text
    assert "fixture-codex-model" in text
    parsed = parse_codex_session_file(session_file, {"codex-fixture-1": "Fixture Codex Task"})
    assert parsed is not None
    assert [message.role for message in parsed.record.messages] == ["user", "assistant"]
    assert [message.model for message in parsed.record.messages] == [
        "fixture-codex-model",
        "fixture-codex-model",
    ]
    assert 'turn_models: ["fixture-codex-model", "fixture-codex-model"]' in text

    unchanged = export_codex(
        output_dir,
        state,
        full=False,
        dry_run=False,
        since_date=None,
        session_dirs=(session_dir,),
        session_index=index_file,
    )
    assert unchanged["exported"] == 0

    _write_codex_session(session_dir, index_file, include_followup=True)
    updated = export_codex(
        output_dir,
        state,
        full=False,
        dry_run=False,
        since_date=None,
        session_dirs=(session_dir,),
        session_index=index_file,
    )
    assert updated["exported"] == 1
    assert len(list(output_dir.glob("*.md"))) == 1
    updated_text = files[0].read_text(encoding="utf-8")
    assert "No further issues." in updated_text
    assert (
        'turn_models: ["fixture-codex-model", "fixture-codex-model", '
        '"fixture-codex-model-2", "fixture-codex-model-2"]' in updated_text
    )


def test_codex_model_context_after_user_backfills_turn(tmp_path: Path) -> None:
    session_file = tmp_path / "rollout-2026-06-29T09-00-00-codex-delayed.jsonl"
    events = [
        {
            "timestamp": "2026-06-29T09:00:00Z",
            "type": "session_meta",
            "payload": {"id": "codex-delayed", "cwd": "/home/user/project"},
        },
        {
            "timestamp": "2026-06-29T09:00:01Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "Review the delayed fixture"},
        },
        {
            "timestamp": "2026-06-29T09:00:02Z",
            "type": "turn_context",
            "payload": {"model": "fixture-delayed-model"},
        },
        {
            "timestamp": "2026-06-29T09:00:03Z",
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": "Reviewed."},
        },
    ]
    session_file.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

    parsed = parse_codex_session_file(session_file, {})

    assert parsed is not None
    assert [message.model for message in parsed.record.messages] == [
        "fixture-delayed-model",
        "fixture-delayed-model",
    ]


def test_cursor_export_with_fixture(tmp_path: Path) -> None:
    db_path = tmp_path / "state.vscdb"
    _seed_cursor_db(db_path)

    state = {"cursor": {"sessions": {}}}
    result = export_cursor(
        tmp_path / "cursor", state, db_path=db_path, full=False, dry_run=False, since_date=None
    )
    assert result == {"source": "cursor", "scanned": 1, "exported": 1}

    files = list((tmp_path / "cursor").glob("*.md"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert "source: cursor" in text
    assert 'session_id: "a6f723dc-9c5b-4169-b03f-31abb1e6069b"' in text
    assert 'title: "Fixture Cursor Session"' in text
    assert 'project_directory: "/home/user/project"' in text
    assert "Review the fixture code" in text
    assert "The fixture looks good." in text
    assert "fixture-cursor-model" in text
    # The model is captured from the user bubble and carried to its response.
    assert 'turn_models: ["fixture-cursor-model", "fixture-cursor-model"]' in text

    unchanged = export_cursor(
        tmp_path / "cursor", state, db_path=db_path, full=False, dry_run=False, since_date=None
    )
    assert unchanged["exported"] == 0


def test_cursor_export_skips_subagent(tmp_path: Path) -> None:
    db_path = tmp_path / "state.vscdb"
    _seed_cursor_db(db_path)

    conn = sqlite3.connect(str(db_path))
    subagent_id = "11111111-1111-4111-8111-111111111111"
    conn.execute(
        "INSERT INTO composerHeaders (composerId, createdAt, lastUpdatedAt, isSubagent, value) "
        "VALUES (?,?,?,?,?)",
        (
            subagent_id,
            0,
            0,
            1,
            json.dumps({"name": "Explore subagent", "workspaceIdentifier": {"uri": {"fsPath": ""}}}),
        ),
    )
    conn.execute(
        "INSERT INTO cursorDiskKV (key, value) VALUES (?,?)",
        (
            f"bubbleId:{subagent_id}:00000000-0000-0000-0000-000000000000",
            json.dumps({"type": 1, "text": "subagent chatter", "createdAt": "2026-06-29T09:00:00.000Z"}),
        ),
    )
    conn.commit()
    conn.close()

    state = {"cursor": {"sessions": {}}}
    result = export_cursor(
        tmp_path / "cursor", state, db_path=db_path, full=False, dry_run=False, since_date=None
    )
    assert result["exported"] == 1
    files = list((tmp_path / "cursor").glob("*.md"))
    assert len(files) == 1
    assert "subagent chatter" not in files[0].read_text(encoding="utf-8")


DSH_FIXTURE_SESSION_ID = "session-d5a4b3c2-1111-4222-8333-444455556666"
DSH_FIXTURE_CREATED_AT = 1786740000000


def _dsh_fixture_events() -> list[dict[str, object]]:
    return [
        {
            "type": "session",
            "version": 0,
            "id": DSH_FIXTURE_SESSION_ID,
            "createdAt": DSH_FIXTURE_CREATED_AT,
            "cwd": "/home/user/project",
            "agentPreset": "standard",
        },
        {"type": "permission/preset", "seq": 0, "time": DSH_FIXTURE_CREATED_AT + 1, "data": {"preset": "default"}},
        {
            "type": "user/message",
            "seq": 1,
            "time": DSH_FIXTURE_CREATED_AT + 100,
            "data": {
                "content": [{"type": "text", "text": "Instrument the DSH fixture"}],
                "role": "user",
            },
        },
        {"type": "turn/start", "seq": 2, "time": DSH_FIXTURE_CREATED_AT + 101, "data": {"turn": 1}},
        {
            "type": "request/header",
            "seq": 3,
            "time": DSH_FIXTURE_CREATED_AT + 102,
            "data": {"header": {"config": {"provider": "fixture-provider", "model": "fixture-model"}}},
        },
        {
            "type": "assistant/message",
            "seq": 4,
            "time": DSH_FIXTURE_CREATED_AT + 200,
            "data": {
                "message": {
                    "content": [
                        {"type": "reasoning", "text": "internal reasoning must not leak"},
                        {"type": "text", "text": "The fixture is wired correctly."},
                    ],
                    "role": "assistant",
                    "source": {"kind": "model", "provider": "fixture-provider", "model": "fixture-model"},
                }
            },
        },
        {
            "type": "assistant/message",
            "seq": 5,
            "time": DSH_FIXTURE_CREATED_AT + 300,
            "data": {
                "message": {
                    "content": [{"type": "text", "text": "Second step narration."}],
                    "role": "assistant",
                    "source": {"kind": "model", "provider": "fixture-provider", "model": "fixture-model"},
                }
            },
        },
        {
            "type": "session/title",
            "seq": 6,
            "time": DSH_FIXTURE_CREATED_AT + 400,
            "data": {"title": "fallback title", "source": {"kind": "fallback"}},
        },
        {
            "type": "session/title",
            "seq": 7,
            "time": DSH_FIXTURE_CREATED_AT + 500,
            "data": {"title": "DSH fixture title", "source": {"kind": "provider"}},
        },
    ]


def _write_dsh_session(
    sessions_dir: Path,
    *,
    session_id: str = DSH_FIXTURE_SESSION_ID,
    origin: str = "",
    compressed: bool = False,
    torn_tail: bool = False,
) -> Path:
    events = _dsh_fixture_events()
    if origin:
        events[0] = {**events[0], "origin": origin, "delegationDepth": 1}
    text = "\n".join(json.dumps(event) for event in events) + "\n"
    if torn_tail:
        text += '{"type": "assistant/message", "seq": 7, "time": '
    session_dir = sessions_dir / "--home-user-project--" / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    file_path = session_dir / ("session.jsonl.zstd" if compressed else "session.jsonl")
    if compressed:
        file_path.write_bytes(
            subprocess.run(["zstd", "-c", "-"], input=text.encode(), capture_output=True, check=True).stdout
        )
    else:
        file_path.write_text(text, encoding="utf-8")
    return file_path


def test_dsh_export_with_fixture(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    _write_dsh_session(sessions_dir, torn_tail=True)

    state = {"dsh": {"sessions": {}}}
    result = export_dsh(
        tmp_path / "dsh",
        state,
        full=False,
        dry_run=False,
        since_date=None,
        sessions_dir=sessions_dir,
    )
    assert result["exported"] == 1

    files = list((tmp_path / "dsh").glob("*.md"))
    assert len(files) == 1
    content = files[0].read_text(encoding="utf-8")
    assert "source: dsh" in content
    assert "DSH fixture title" in content
    assert 'project_directory: "/home/user/project"' in content
    assert 'models_used: ["fixture-provider/fixture-model"]' in content
    assert 'turn_models: ["fixture-provider/fixture-model", "fixture-provider/fixture-model", "fixture-provider/fixture-model"]' in content
    assert "## User [" in content
    assert "The fixture is wired correctly." in content
    assert "Second step narration." in content
    # Reasoning blocks and torn tails never reach the archive.
    assert "internal reasoning" not in content

    # Incremental cursor recorded per session.
    assert state["dsh"]["sessions"][DSH_FIXTURE_SESSION_ID]["latest_timestamp"] > 0


def test_dsh_growing_session_rewrites_one_file(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    file_path = _write_dsh_session(sessions_dir)
    state = {"dsh": {"sessions": {}}}

    export_dsh(tmp_path / "dsh", state, full=False, dry_run=False, since_date=None, sessions_dir=sessions_dir)
    first_files = sorted(path.name for path in (tmp_path / "dsh").glob("*.md"))
    assert len(first_files) == 1

    # The live session grows: append a later turn and re-export.
    grown = file_path.read_text(encoding="utf-8") + json.dumps(
        {
            "type": "user/message",
            "seq": 8,
            "time": DSH_FIXTURE_CREATED_AT + 5000,
            "data": {"content": [{"type": "text", "text": "Follow-up after growth"}], "role": "user"},
        }
    ) + "\n"
    file_path.write_text(grown, encoding="utf-8")

    result = export_dsh(tmp_path / "dsh", state, full=False, dry_run=False, since_date=None, sessions_dir=sessions_dir)
    assert result["exported"] == 1
    files = sorted(path.name for path in (tmp_path / "dsh").glob("*.md"))
    assert files == first_files  # same output file rewritten, no _2 duplicate
    assert "Follow-up after growth" in (tmp_path / "dsh" / files[0]).read_text(encoding="utf-8")


def test_dsh_subagent_session_is_skipped(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    _write_dsh_session(sessions_dir, session_id="session-sub-00000000-1111-4222-8333-444455556666", origin="subagent")

    state = {"dsh": {"sessions": {}}}
    result = export_dsh(
        tmp_path / "dsh", state, full=False, dry_run=False, since_date=None, sessions_dir=sessions_dir
    )
    assert result["exported"] == 0
    assert not list((tmp_path / "dsh").glob("*.md"))


def test_dsh_system_reminder_injection_is_dropped(tmp_path: Path) -> None:
    session_dir = tmp_path / "sessions" / "--home-user-project--" / DSH_FIXTURE_SESSION_ID
    session_dir.mkdir(parents=True)
    lines = [
        json.dumps({"type": "session", "version": 0, "id": DSH_FIXTURE_SESSION_ID, "createdAt": DSH_FIXTURE_CREATED_AT, "cwd": "/home/user/project"}),
        json.dumps({
            "type": "user/message",
            "seq": 1,
            "time": DSH_FIXTURE_CREATED_AT + 100,
            "data": {"content": [{"type": "text", "text": "Real question about the fixture"}], "role": "user"},
        }),
        json.dumps({
            "type": "user/message",
            "seq": 2,
            "time": DSH_FIXTURE_CREATED_AT + 101,
            "data": {"content": [{"type": "text", "text": "<system-reminder>\nInjected workspace instructions.\n</system-reminder>"}], "role": "user"},
        }),
        json.dumps({
            "type": "user/message",
            "seq": 3,
            "time": DSH_FIXTURE_CREATED_AT + 102,
            "data": {"content": [{"type": "text", "text": "<system-reminder>note</system-reminder>\nActual text beside a reminder"}], "role": "user"},
        }),
        json.dumps({
            "type": "user/message",
            "seq": 4,
            "time": DSH_FIXTURE_CREATED_AT + 103,
            "data": {
                "content": [
                    {
                        "type": "text",
                        "text": "Current runtime context. This snapshot supersedes earlier runtime-context snapshots.\n\nMode: danger-full-access.",
                    }
                ],
                "role": "user",
            },
        }),
    ]
    (session_dir / "session.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    parsed = parse_dsh_session_file(session_dir / "session.jsonl")
    assert parsed is not None
    assert [message.content for message in parsed.record.messages] == [
        "Real question about the fixture",
        "Actual text beside a reminder",
    ]


def test_dsh_bare_uuid_directory_is_discovered(tmp_path: Path) -> None:
    """Session directory ids are not a single namespace; discovery must not
    require the `session-` prefix (subagent children use bare uuids, and a
    future harness may mint other shapes)."""
    sessions_dir = tmp_path / "sessions"
    _write_dsh_session(sessions_dir, session_id="7d7c1f46-1111-4222-8333-444455556666")

    state = {"dsh": {"sessions": {}}}
    result = export_dsh(
        tmp_path / "dsh", state, full=False, dry_run=False, since_date=None, sessions_dir=sessions_dir
    )
    assert result["scanned"] == 1
    assert result["exported"] == 1


@pytest.mark.skipif(shutil.which("zstd") is None, reason="zstd binary not available")
def test_dsh_torn_compressed_tail_still_exports_complete_frames(tmp_path: Path) -> None:
    """A truncated final Zstandard frame must not fail the session: the durable
    earlier frames are complete and exportable (crash / mid-append state)."""
    first_part = "\n".join(json.dumps(event) for event in _dsh_fixture_events()) + "\n"
    second_part = json.dumps(
        {
            "type": "user/message",
            "seq": 20,
            "time": DSH_FIXTURE_CREATED_AT + 9000,
            "data": {"content": [{"type": "text", "text": "Written after the last complete frame"}], "role": "user"},
        }
    ) + "\n"
    frame_one = subprocess.run(["zstd", "-c", "-"], input=first_part.encode(), capture_output=True, check=True).stdout
    frame_two = subprocess.run(["zstd", "-c", "-"], input=second_part.encode(), capture_output=True, check=True).stdout

    session_dir = tmp_path / "sessions" / "--home-user-project--" / DSH_FIXTURE_SESSION_ID
    session_dir.mkdir(parents=True)
    (session_dir / "session.jsonl.zstd").write_bytes(frame_one + frame_two[:-8])

    state = {"dsh": {"sessions": {}}}
    result = export_dsh(
        tmp_path / "dsh", state, full=False, dry_run=False, since_date=None, sessions_dir=tmp_path / "sessions"
    )
    assert result["exported"] == 1
    assert "failed" not in result
    content = next(iter((tmp_path / "dsh").glob("*.md"))).read_text(encoding="utf-8")
    assert "The fixture is wired correctly." in content


def test_dsh_model_attribution_without_request_header(tmp_path: Path) -> None:
    """Per-message `source` attribution covers sessions where request/header is
    absent or sparse; user turns inherit the next assistant response's model."""
    lines = [
        json.dumps({"type": "session", "version": 0, "id": DSH_FIXTURE_SESSION_ID, "createdAt": DSH_FIXTURE_CREATED_AT, "cwd": "/home/user/project"}),
        json.dumps({
            "type": "user/message",
            "seq": 1,
            "time": DSH_FIXTURE_CREATED_AT + 100,
            "data": {"content": [{"type": "text", "text": "Which model are you?"}], "role": "user"},
        }),
        json.dumps({
            "type": "assistant/message",
            "seq": 2,
            "time": DSH_FIXTURE_CREATED_AT + 200,
            "data": {
                "message": {
                    "content": [{"type": "text", "text": "fixture-model, at your service."}],
                    "role": "assistant",
                    "source": {"kind": "model", "provider": "fixture-provider", "model": "fixture-model"},
                }
            },
        }),
    ]
    session_dir = tmp_path / "sessions" / "--home-user-project--" / DSH_FIXTURE_SESSION_ID
    session_dir.mkdir(parents=True)
    (session_dir / "session.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    parsed = parse_dsh_session_file(session_dir / "session.jsonl")
    assert parsed is not None
    assert [message.model for message in parsed.record.messages] == [
        "fixture-provider/fixture-model",
        "fixture-provider/fixture-model",
    ]


def test_dsh_since_date_filters_sessions(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    _write_dsh_session(sessions_dir)
    fixture_date = date.fromisoformat(ms_to_date(DSH_FIXTURE_CREATED_AT))

    state = {"dsh": {"sessions": {}}}
    after = export_dsh(
        tmp_path / "dsh", state, full=False, dry_run=False, since_date=fixture_date + timedelta(days=1), sessions_dir=sessions_dir
    )
    assert after["exported"] == 0

    before = export_dsh(
        tmp_path / "dsh", state, full=False, dry_run=False, since_date=fixture_date - timedelta(days=1), sessions_dir=sessions_dir
    )
    assert before["exported"] == 1


def test_dsh_missing_header_or_messages_is_skipped(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"

    no_header_dir = sessions_dir / "--home-user-project--" / "session-noheader00-1111-4222-8333-444455556666"
    no_header_dir.mkdir(parents=True)
    (no_header_dir / "session.jsonl").write_text(
        json.dumps({"type": "user/message", "seq": 1, "time": DSH_FIXTURE_CREATED_AT, "data": {"content": [{"type": "text", "text": "hello"}], "role": "user"}}) + "\n",
        encoding="utf-8",
    )

    header_only_dir = sessions_dir / "--home-user-project--" / "session-headeronly0-1111-4222-8333-444455556666"
    header_only_dir.mkdir(parents=True)
    (header_only_dir / "session.jsonl").write_text(
        json.dumps({"type": "session", "version": 0, "id": "session-headeronly0-1111-4222-8333-444455556666", "createdAt": DSH_FIXTURE_CREATED_AT, "cwd": "/home/user/project"}) + "\n",
        encoding="utf-8",
    )

    assert parse_dsh_session_file(no_header_dir / "session.jsonl") is None
    assert parse_dsh_session_file(header_only_dir / "session.jsonl") is None


@pytest.mark.skipif(shutil.which("zstd") is None, reason="zstd binary not available")
def test_dsh_both_encodings_in_one_directory_export_once(tmp_path: Path) -> None:
    """A configuration change can leave both physical encodings in one session
    directory; the session must be exported exactly once."""
    sessions_dir = tmp_path / "sessions"
    file_path = _write_dsh_session(sessions_dir)
    compressed = subprocess.run(
        ["zstd", "-c", "-"], input=file_path.read_bytes(), capture_output=True, check=True
    ).stdout
    (file_path.parent / "session.jsonl.zstd").write_bytes(compressed)

    state = {"dsh": {"sessions": {}}}
    result = export_dsh(
        tmp_path / "dsh", state, full=False, dry_run=False, since_date=None, sessions_dir=sessions_dir
    )
    assert result["exported"] == 1
    assert len(list((tmp_path / "dsh").glob("*.md"))) == 1


def test_dsh_zero_timestamps_is_skipped(tmp_path: Path) -> None:
    session_dir = tmp_path / "sessions" / "--home-user-project--" / DSH_FIXTURE_SESSION_ID
    session_dir.mkdir(parents=True)
    lines = [
        json.dumps({"type": "session", "version": 0, "id": DSH_FIXTURE_SESSION_ID, "createdAt": 0, "cwd": "/home/user/project"}),
        json.dumps({"type": "user/message", "seq": 1, "data": {"content": [{"type": "text", "text": "no clock"}], "role": "user"}}),
    ]
    (session_dir / "session.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert parse_dsh_session_file(session_dir / "session.jsonl") is None


@pytest.mark.skipif(shutil.which("zstd") is None, reason="zstd binary not available")
def test_dsh_compressed_session_exports(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    _write_dsh_session(sessions_dir, compressed=True)

    state = {"dsh": {"sessions": {}}}
    result = export_dsh(
        tmp_path / "dsh", state, full=False, dry_run=False, since_date=None, sessions_dir=sessions_dir
    )
    assert result["exported"] == 1
    content = next((tmp_path / "dsh").glob("*.md")).read_text(encoding="utf-8")
    assert "The fixture is wired correctly." in content


@pytest.mark.skipif(shutil.which("zstd") is None, reason="zstd binary not available")
def test_dsh_unreadable_session_is_isolated(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    _write_dsh_session(sessions_dir, session_id="session-good-000000-1111-4222-8333-444455556666")
    bad_dir = sessions_dir / "--home-user-broken--" / "session-bad00000-1111-4222-8333-444455556666"
    bad_dir.mkdir(parents=True)
    (bad_dir / "session.jsonl.zstd").write_bytes(b"not a zstd frame")

    state = {"dsh": {"sessions": {}}}
    result = export_dsh(
        tmp_path / "dsh", state, full=False, dry_run=False, since_date=None, sessions_dir=sessions_dir
    )
    assert result["exported"] == 1
    assert result["failed"] == 1
    assert "RuntimeError" in result["warnings"][0]["error"]
    assert len(list((tmp_path / "dsh").glob("*.md"))) == 1


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

    codex_dir = tmp_path / "codex_sessions"
    codex_index = tmp_path / "codex_session_index.jsonl"
    _write_codex_session(codex_dir, codex_index)

    cursor_db = tmp_path / "cursor.vscdb"
    _seed_cursor_db(cursor_db)

    dsh_dir = tmp_path / "dsh_sessions"
    _write_dsh_session(dsh_dir)

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
        codex_session_dirs=(codex_dir,),
        codex_session_index=codex_index,
        cursor_db=cursor_db,
        dsh_sessions_dir=dsh_dir,
    )

    assert {r["source"] for r in results} == {
        "second_mind",
        "opencode",
        "claude_code",
        "antigravity",
        "codex",
        "cursor",
        "dsh",
    }

    # Each source produced at least one markdown file under base_dir.
    for sub in ("second_mind", "opencode", "claude_code", "antigravity", "codex", "cursor", "dsh"):
        assert list((tmp_path / sub).glob("*.md")), f"no markdown emitted for {sub}"

    # State file was persisted with refreshed counters.
    persisted = load_state(state_file)
    assert persisted["second_mind"]["last_export_count"] == 1
    assert persisted["opencode"]["last_session_time"] > 0
    assert persisted["claude_code"]["last_timestamp"] > 0
    antigravity_sessions = persisted["antigravity"]["surfaces"]["ide"]["sessions"]
    assert antigravity_sessions["antigravity-session-fixture"]["status"] == "complete"
    assert persisted["codex"]["sessions"]["codex-fixture-1"]["latest_timestamp"] > 0
    assert persisted["cursor"]["sessions"]["a6f723dc-9c5b-4169-b03f-31abb1e6069b"]["latest_timestamp"] > 0
    assert persisted["dsh"]["sessions"][DSH_FIXTURE_SESSION_ID]["latest_timestamp"] > 0


def test_cli_main_reports_partial_antigravity_failure(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    args = SimpleNamespace(
        source="antigravity",
        full=False,
        dry_run=False,
        base_dir=Path("/tmp/example-output"),
        state_file=Path("/tmp/example-state.json"),
        second_mind_json=Path("/tmp/example-second-mind.json"),
        opencode_db=Path("/tmp/example-opencode.db"),
        antigravity_dir=None,
        codex_dir=None,
        codex_session_index=Path("/tmp/example-codex-index.jsonl"),
        cursor_db=Path("/tmp/example-cursor.db"),
        dsh_sessions_dir=Path("/tmp/example-dsh-sessions"),
        since_date=None,
    )
    monkeypatch.setattr(cli_module, "parse_args", lambda: args)
    monkeypatch.setattr(
        cli_module,
        "run_export",
        lambda *args, **kwargs: [
            {
                "source": "antigravity",
                "scanned": 2,
                "exported": 1,
                "failed": 1,
                "warnings": [
                    {
                        "surface": "cli",
                        "line": 7,
                        "error": "Expected a JSON object",
                    }
                ],
            }
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli_module.main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "exported=1 scanned=2 failed=1" in captured.out
    assert "[antigravity:cli] line 7: Expected a JSON object" in captured.err
    assert "fixture-session" not in captured.err
    assert "/tmp/" not in captured.err


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
        brain_dirs = {surface: path for surface, path in DEFAULT_ANTIGRAVITY_BRAIN_DIRS.items() if path.is_dir()}
        if not brain_dirs:
            pytest.skip("No Antigravity brain directories found")

        since = date.today() - timedelta(days=7)

        # Dry-run first: scan only, no files written.
        export_antigravity(
            tmp_path / "dry",
            {},
            brain_dirs=brain_dirs,
            full=True,
            dry_run=True,
            since_date=since,
        )

        # Real export.
        result = export_antigravity(
            tmp_path / "antigravity",
            {},
            brain_dirs=brain_dirs,
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

    def test_live_codex_export(self, tmp_path: Path) -> None:
        """Export recent Codex sessions without exposing transcript content."""
        if not any(path.is_dir() for path in DEFAULT_CODEX_SESSION_DIRS):
            pytest.skip("No Codex session directories found")

        since = date.today() - timedelta(days=7)
        result = export_codex(
            tmp_path / "codex",
            {},
            full=True,
            dry_run=False,
            since_date=since,
            session_dirs=DEFAULT_CODEX_SESSION_DIRS,
            session_index=DEFAULT_CODEX_SESSION_INDEX,
        )
        files = list((tmp_path / "codex").glob("*.md"))
        if result["exported"] == 0:
            pytest.skip("No recent Codex sessions to export")
        assert len(files) == result["exported"]
        assert "source: codex" in files[0].read_text(encoding="utf-8")

    def test_live_cursor_export(self, tmp_path: Path) -> None:
        """Export recent Cursor sessions without exposing transcript content."""
        if not DEFAULT_CURSOR_DB.exists():
            pytest.skip(f"Cursor DB not found: {DEFAULT_CURSOR_DB}")

        since = date.today() - timedelta(days=7)
        result = export_cursor(
            tmp_path / "cursor",
            {},
            db_path=DEFAULT_CURSOR_DB,
            full=True,
            dry_run=False,
            since_date=since,
        )
        files = list((tmp_path / "cursor").glob("*.md"))
        if result["exported"] == 0:
            pytest.skip("No recent Cursor sessions to export")
        assert len(files) == result["exported"]
        assert "source: cursor" in files[0].read_text(encoding="utf-8")

    def test_live_dsh_export(self, tmp_path: Path) -> None:
        """Export recent DeepSeek Harness sessions without exposing transcript content."""
        if not DEFAULT_DSH_SESSIONS_DIR.is_dir():
            pytest.skip(f"DSH sessions directory not found: {DEFAULT_DSH_SESSIONS_DIR}")

        since = date.today() - timedelta(days=7)
        result = export_dsh(
            tmp_path / "dsh",
            {},
            full=True,
            dry_run=False,
            since_date=since,
            sessions_dir=DEFAULT_DSH_SESSIONS_DIR,
        )
        files = list((tmp_path / "dsh").glob("*.md"))
        if result["exported"] == 0:
            pytest.skip("No recent DSH sessions to export")
        assert len(files) == result["exported"]
        assert "source: dsh" in files[0].read_text(encoding="utf-8")
