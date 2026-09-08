from __future__ import annotations

import copy
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_session_export.sources.gemini import export_gemini, parse_gemini_session_file
from ai_session_export.sources.grok import export_grok, parse_grok_session_file


TIME = "2026-06-10T12:00:00Z"


def gemini_message(message_id, kind, text, **extra):
    return {"id": message_id, "timestamp": TIME, "type": kind, "content": text, **extra}


def gemini_fixture(root: Path, suffix=".json") -> Path:
    path = root / "example-project-hash" / "chats" / ("session-example" + suffix)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "sessionId": "gemini-fixture", "projectHash": "example-project-hash",
        "startTime": TIME, "lastUpdated": TIME,
        "messages": [
            gemini_message("u1", "user", "expanded prompt with machine context", displayContent="Explain synthetic foxes"),
            gemini_message("a1", "gemini", [{"text": "A fox "}, {"text": "can be synthetic."},
                                          {"text": "thought-secret", "thought": True},
                                          {"functionCall": {"name": "tool-secret"}}],
                           model="gemini-example-1", thoughts=[{"text": "thought-secret"}],
                           toolCalls=[{"result": "tool-secret"}]),
            gemini_message("info1", "info", "info-secret"),
            gemini_message("u2", "user", [{"text": "And owls?"}, {"inlineData": {"data": "binary-secret"}}]),
            gemini_message("a2", "gemini", "Owls too.", model="gemini-example-2"),
        ],
    }
    if suffix == ".json":
        path.write_text(json.dumps(data, indent=2))
    else:
        messages = data.pop("messages")
        path.write_text("\n".join(map(json.dumps, [data, *messages])) + "\n")
    return path


def grok_event(kind, text="", *, index=None, timestamp=1781092800, **extra):
    update = {"sessionUpdate": kind, **extra}
    if kind.endswith("_chunk"):
        update["content"] = {"type": "text", "text": text}
    if index is not None:
        update["_meta"] = {"promptIndex": index}
    return {"timestamp": timestamp, "method": "session/update",
            "params": {"sessionId": "grok-fixture", "update": update}}


def grok_fixture(root: Path) -> Path:
    path = root / "%2Fexample%2Fproject" / "grok-fixture" / "updates.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.with_name("summary.json").write_text(json.dumps({
        "info": {"id": "grok-fixture", "cwd": "/example/project"},
        "created_at": TIME, "updated_at": TIME, "session_summary": "Synthetic foxes",
        "generated_title": "Grok fixture", "current_model_id": "grok-example",
        "session_kind": "fork", "parent_session_id": "original-session",
    }))
    events = [
        grok_event("user_message_chunk", "Explain ", index=0),
        grok_event("user_message_chunk", "synthetic foxes", index=0),
        grok_event("agent_thought_chunk", "thought-secret"),
        grok_event("agent_message_chunk", "A fox "),
        grok_event("agent_message_chunk", "can be synthetic."),
        grok_event("tool_call", title="tool-secret"),
        grok_event("user_message_chunk", "And owls?", index=1),
        grok_event("agent_message_chunk", "Owls too."),
    ]
    path.write_text("\n".join(map(json.dumps, events)) + "\n")
    return path


@pytest.mark.parametrize("suffix", [".json", ".jsonl"])
def test_gemini_public_formats_filter_and_attribute(suffix, tmp_path):
    record = parse_gemini_session_file(gemini_fixture(tmp_path, suffix))
    assert record.session_id == "gemini-fixture"
    assert record.date == "2026-06-10"
    assert record.project_directory == ""
    assert [m.content for m in record.messages] == ["Explain synthetic foxes", "A fox can be synthetic.", "And owls?", "Owls too."]
    assert [m.model for m in record.messages] == ["gemini-example-1"] * 2 + ["gemini-example-2"] * 2
    assert all(m.time_created for m in record.messages)


def test_gemini_jsonl_replacements_rewind_and_checkpoint(tmp_path):
    path = gemini_fixture(tmp_path, ".jsonl")
    with path.open("a") as handle:
        for update in [gemini_message("a2", "gemini", "Updated owl.", model="gemini-example-2"),
                       {"$rewindTo": "u2"}, gemini_message("u3", "user", "New question")]:
            handle.write(json.dumps(update) + "\n")
        handle.write('{"incomplete":')
    record = parse_gemini_session_file(path)
    assert [m.content for m in record.messages] == ["Explain synthetic foxes", "A fox can be synthetic.", "New question"]
    with path.open("a") as handle:
        handle.write("\n" + json.dumps({"$set": {"summary": "Checkpoint title", "messages": [
            gemini_message("u4", "user", "Checkpoint question"), gemini_message("a4", "gemini", "Checkpoint answer")
        ]}}) + "\n")
    record = parse_gemini_session_file(path)
    assert record.title == "Checkpoint title"
    assert [m.content for m in record.messages] == ["Checkpoint question", "Checkpoint answer"]


def test_gemini_missing_model_does_not_leak_future_model(tmp_path):
    path = gemini_fixture(tmp_path)
    data = json.loads(path.read_text())
    del data["messages"][1]["model"]
    path.write_text(json.dumps(data))
    record = parse_gemini_session_file(path)
    assert [m.model for m in record.messages] == [None, None, "gemini-example-2", "gemini-example-2"]


def test_gemini_prefers_migrated_jsonl_and_skips_subagents(tmp_path):
    gemini_fixture(tmp_path)
    path = gemini_fixture(tmp_path, ".jsonl")
    with path.open("a") as handle:
        handle.write(json.dumps({"$set": {"kind": "subagent"}}) + "\n")
    assert export_gemini(tmp_path / "out", {}, full=False, dry_run=False, since_date=None,
                         gemini_dir=tmp_path)["exported"] == 0


def test_grok_public_format_stitches_and_keeps_forks(tmp_path):
    record = parse_grok_session_file(grok_fixture(tmp_path))
    assert record.session_id == "grok-fixture"
    assert record.project_directory == "/example/project"
    assert record.title == "Grok fixture"
    assert [m.content for m in record.messages] == ["Explain synthetic foxes", "A fox can be synthetic.", "And owls?", "Owls too."]
    assert record.models_used == ["grok-example"]
    assert all(m.model is None for m in record.messages)
    assert all(m.time_created for m in record.messages)


def test_grok_rewind_and_legacy_records(tmp_path):
    path = grok_fixture(tmp_path)
    with path.open("a") as handle:
        handle.write(json.dumps({"method": "_x.ai/session/update", "params": {"sessionId": "grok-fixture",
            "update": {"sessionUpdate": "rewind_marker", "target_prompt_index": 1}}}) + "\n")
        handle.write(json.dumps(grok_event("user_message_chunk", "New question", index=1)["params"]) + "\n")
        handle.write(json.dumps(grok_event("agent_message_chunk", "New answer")["params"]) + "\n")
        handle.write('{"torn":')
    assert [m.content for m in parse_grok_session_file(path).messages] == [
        "Explain synthetic foxes", "A fox can be synthetic.", "New question", "New answer"]


@pytest.mark.parametrize("first_index", [None, 0])
def test_grok_rewind_preserves_answer_after_unmarked_mid_turn_context(first_index, tmp_path):
    path = grok_fixture(tmp_path)
    events = [
        grok_event("user_message_chunk", "First question", index=first_index),
        grok_event("agent_message_chunk", "First answer"),
        grok_event("user_message_chunk", "Second question", index=1),
        grok_event("agent_message_chunk", "Beginning second answer"),
        grok_event("user_message_chunk", "Unmarked mid-turn context"),
        grok_event("agent_message_chunk", "Final second answer"),
        grok_event("user_message_chunk", "Abandoned question", index=2),
        grok_event("agent_message_chunk", "Abandoned answer"),
        {"method": "_x.ai/session/update", "params": {"sessionId": "grok-fixture",
            "update": {"sessionUpdate": "rewind_marker", "target_prompt_index": 2}}},
        grok_event("user_message_chunk", "Replacement question", index=2),
        grok_event("agent_message_chunk", "Replacement answer"),
    ]
    path.write_text("\n".join(map(json.dumps, events)) + "\n")
    assert [message.content for message in parse_grok_session_file(path).messages] == [
        "First question", "First answer", "Second question", "Beginning second answer",
        "Unmarked mid-turn context", "Final second answer", "Replacement question", "Replacement answer",
    ]


@pytest.mark.parametrize("field", ["info", "chunk_meta", "content", "content_meta", "notification_meta"])
def test_grok_wrong_shaped_session_is_isolated_and_retryable(field, tmp_path):
    root, output, state = tmp_path / "input", tmp_path / "output", {}
    good = grok_fixture(root)
    bad = good.parent.parent / "malformed" / "updates.jsonl"
    bad.parent.mkdir()
    summary = json.loads(good.with_name("summary.json").read_text())
    summary["info"]["id"] = "malformed-fixture"
    events = [json.loads(line) for line in good.read_text().splitlines()]
    for event in events:
        event["params"]["sessionId"] = "malformed-fixture"
    valid_summary, valid_events = copy.deepcopy(summary), copy.deepcopy(events)
    update = events[0]["params"]["update"]
    if field == "info":
        summary["info"] = ["wrong shape"]
    elif field == "chunk_meta":
        update["_meta"] = ["wrong shape"]
    elif field == "content":
        update["content"] = ["wrong shape"]
    elif field == "content_meta":
        update["content"]["_meta"] = ["wrong shape"]
    else:
        events[0]["params"]["_meta"] = ["wrong shape"]
    bad.with_name("summary.json").write_text(json.dumps(summary))
    bad.write_text("\n".join(map(json.dumps, events)) + "\n")
    kwargs = {"full": False, "since_date": None, "sessions_dir": root}
    assert export_grok(output, state, dry_run=True, **kwargs) == {
        "source": "grok", "scanned": 2, "exported": 1, "failed": 1,
    }
    assert state == {} and not output.exists()
    assert export_grok(output, state, dry_run=False, **kwargs)["failed"] == 1
    assert len(list(output.glob("*.md"))) == 1
    assert set(state["grok"]["sessions"]) == {"grok-fixture"}
    bad.with_name("summary.json").write_text(json.dumps(valid_summary))
    bad.write_text("\n".join(map(json.dumps, valid_events)) + "\n")
    assert export_grok(output, state, dry_run=False, **kwargs) == {
        "source": "grok", "scanned": 2, "exported": 1,
    }
    assert len(list(output.glob("*.md"))) == 2


def test_grok_timestamped_log_dates_legacy_summary(tmp_path):
    path = grok_fixture(tmp_path)
    summary = json.loads(path.with_name("summary.json").read_text())
    del summary["created_at"]
    path.with_name("summary.json").write_text(json.dumps(summary))
    record = parse_grok_session_file(path)
    assert record.date == "2026-06-10"


def test_grok_display_content_and_hidden_host_messages(tmp_path):
    path = grok_fixture(tmp_path)
    display = grok_event("user_message_chunk", "injected context", index=2)
    display["params"]["update"]["content"]["_meta"] = {"displayText": "Human wording"}
    hidden = grok_event("user_message_chunk", "hidden-secret")
    hidden["params"]["update"]["_meta"] = {"hideFromScrollback": True, "hostTurn": True}
    shell = grok_event("user_message_chunk", "shell-secret")
    shell["params"]["update"]["content"]["_meta"] = {"bash_command": "echo example"}
    with path.open("a") as handle:
        for event in [display, hidden, shell]:
            handle.write(json.dumps(event) + "\n")
    record = parse_grok_session_file(path)
    assert record.messages[-1].content == "Human wording"
    assert "secret" not in str(record)


@pytest.mark.parametrize("kind", ["subagent", "subagent_fork"])
def test_grok_skips_subagents(kind, tmp_path):
    path = grok_fixture(tmp_path)
    summary = json.loads(path.with_name("summary.json").read_text())
    summary["session_kind"] = kind
    path.with_name("summary.json").write_text(json.dumps(summary))
    assert parse_grok_session_file(path) is None


@pytest.mark.parametrize("source,writer,exporter,option", [
    ("gemini", gemini_fixture, export_gemini, "gemini_dir"),
    ("grok", grok_fixture, export_grok, "sessions_dir"),
])
def test_adapter_incremental_growth_rename_dry_run_and_failure(source, writer, exporter, option, tmp_path):
    root, output, state = tmp_path / "input", tmp_path / "output", {}
    path = writer(root)
    kwargs = {option: root, "full": False, "since_date": None}
    assert exporter(output, state, dry_run=True, **kwargs)["exported"] == 1
    assert state == {} and not output.exists()
    assert exporter(output, state, dry_run=False, **kwargs)["exported"] == 1
    first_path = next(output.glob("*.md"))
    assert exporter(output, state, dry_run=False, **kwargs)["exported"] == 0
    if source == "gemini":
        data = json.loads(path.read_text())
        data["summary"] = "Renamed session"
        data["messages"].append(gemini_message("u3", "user", "New follow-up"))
        path.write_text(json.dumps(data))
        bad = path.with_name("session-malformed.json")
    else:
        summary_path = path.with_name("summary.json")
        data = json.loads(summary_path.read_text())
        data["generated_title"] = "Renamed session"
        summary_path.write_text(json.dumps(data))
        with path.open("a") as handle:
            handle.write(json.dumps(grok_event("user_message_chunk", "New follow-up", index=2)) + "\n")
        bad = path.parent.parent / "malformed" / "updates.jsonl"
        bad.parent.mkdir()
    bad.write_text("broken JSON")
    snapshot = copy.deepcopy(state)
    before = first_path.read_bytes()
    assert exporter(output, state, dry_run=True, **kwargs)["failed"] == 1
    assert state == snapshot and first_path.read_bytes() == before
    result = exporter(output, state, dry_run=False, **kwargs)
    assert result["exported"] == 1 and result["failed"] == 1
    assert list(output.glob("*.md")) == [first_path]
    assert "New follow-up" in first_path.read_text() and "Renamed session" in first_path.read_text()
    assert exporter(output, state, dry_run=False, **kwargs)["exported"] == 0
    assert exporter(output, {}, dry_run=True, **{**kwargs, "since_date": date(2026, 6, 11)})["exported"] == 0


@pytest.mark.parametrize("source,writer,flag", [
    ("gemini", gemini_fixture, "--gemini-dir"), ("grok", grok_fixture, "--grok-sessions-dir"),
])
def test_cli_source_registration_and_state_roundtrip(source, writer, flag, tmp_path):
    writer(tmp_path / "input")
    command = [sys.executable, "export_sessions.py", "--source", source, flag, str(tmp_path / "input"),
               "--base-dir", str(tmp_path / "archive"), "--state-file", str(tmp_path / "state.json")]
    repo = Path(__file__).resolve().parents[1]
    first = subprocess.run(command, cwd=repo, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    assert "exported=1 scanned=1" in first.stdout
    assert source in json.loads((tmp_path / "state.json").read_text())
    second = subprocess.run(command, cwd=repo, capture_output=True, text=True)
    assert second.returncode == 0 and "exported=0 scanned=1" in second.stdout
