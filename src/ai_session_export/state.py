from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any


DEFAULT_STATE = {
    "second_mind": {"last_export_count": 0},
    "opencode": {"last_session_time": 0},
    "claude_code": {"sessions": {}},
    "codex": {"sessions": {}},
    "antigravity": {"last_timestamp": 0, "legacy_cursor_migrated": False, "surfaces": {}},
    "cursor": {"sessions": {}},
    "dsh": {"sessions": {}},
    "gemini": {"sessions": {}},
    "grok": {"sessions": {}},
}


def load_state(state_file: Path) -> dict[str, Any]:
    if not state_file.exists():
        return json.loads(json.dumps(DEFAULT_STATE))
    with state_file.open("r", encoding="utf-8") as handle:
        state = json.load(handle)
    for source, defaults in DEFAULT_STATE.items():
        state.setdefault(source, {})
        for key, value in defaults.items():
            state[source].setdefault(key, deepcopy(value))
    return state


def save_state(state: dict[str, Any], state_file: Path) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    temp_file = state_file.with_name(f".{state_file.name}.tmp")
    temp_file.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp_file.replace(state_file)
