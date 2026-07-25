from __future__ import annotations

import json

from .models import SessionRecord
from .utils import ms_to_hhmm, yaml_string


def render_markdown(session: SessionRecord) -> str:
    lines: list[str] = [
        "---",
        f"source: {session.source}",
        f"session_id: {yaml_string(session.session_id)}",
        f"title: {yaml_string(session.title)}",
        f"date: {yaml_string(session.date)}",
        f"message_count: {len(session.messages)}",
    ]
    if session.project_directory:
        lines.append(f"project_directory: {yaml_string(session.project_directory)}")
    if session.models_used:
        lines.append(f"models_used: {json.dumps(session.models_used, ensure_ascii=False)}")
    turn_models = [message.model for message in session.messages]
    if any(turn_models):
        lines.append(f"turn_models: {json.dumps(turn_models, ensure_ascii=False)}")
    lines.extend(["---", "", f"# {session.title}", ""])

    for message in session.messages:
        section = "User" if message.role == "user" else "Assistant"
        if message.time_created:
            lines.append(f"## {section} [{ms_to_hhmm(message.time_created)}]")
        else:
            lines.append(f"## {section}")
        lines.append("")
        lines.append(message.content.rstrip())
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
