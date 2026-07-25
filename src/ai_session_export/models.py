from __future__ import annotations

from typing import NamedTuple


class MessageTurn(NamedTuple):
    role: str
    content: str
    time_created: int | None = None  # ms epoch of the turn's first message, if known
    model: str | None = None  # target/responder model for this turn, if attributable


class SessionRecord(NamedTuple):
    source: str
    session_id: str
    title: str
    date: str
    messages: list[MessageTurn]
    project_directory: str = ""
    models_used: list[str] = []
