"""Состояние моста на диске: линия, отсечка, дедупликация, привязка комнат."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import settings
from app.storage import read_json, write_json

# Сколько идентификаторов сообщений держим для дедупликации.
# Канал Centrifugo recoverable: при переподключении сервер досылает пропущенное,
# поэтому без этой памяти клиент получил бы дубли в Битриксе.
SEEN_LIMIT = 3000


def line_path() -> Path:
    return settings.data_dir / "connector_line.json"


def bridge_path() -> Path:
    return settings.data_dir / "bridge_state.json"


def cookies_path() -> Path:
    return settings.data_dir / "domclick_cookies.json"


def read_line() -> dict[str, Any]:
    return read_json(line_path(), default={}) or {}


def write_line(line: int, active: bool, activation: dict[str, Any] | None = None) -> None:
    payload: dict[str, Any] = {"line": line, "active": active, "updated_at": _now()}
    if activation is not None:
        payload["activation"] = activation
    elif (previous := read_line().get("activation")) is not None:
        payload["activation"] = previous
    write_json(line_path(), payload)


def read_bridge() -> dict[str, Any]:
    return read_json(bridge_path(), default={}) or {}


def start_watching(cutoff_ms: int) -> dict[str, Any]:
    """Фиксирует момент подключения.

    Всё, что старше этой отметки, в Битрикс не переносится: у аккаунта сотни
    старых комнат, и заливать их историю в открытую линию нельзя — операторов
    накроет лавиной, а CRM получит сотни контактов.
    """
    state = read_bridge()
    if not state.get("cutoff_ms"):
        state["cutoff_ms"] = cutoff_ms
        state["started_at"] = _now()
        state.setdefault("seen", [])
        state.setdefault("rooms", {})
        write_json(bridge_path(), state)
    return state


def cutoff_ms() -> int:
    return int(read_bridge().get("cutoff_ms") or 0)


def is_seen(message_id: str) -> bool:
    return message_id in set(read_bridge().get("seen") or [])


def remember(message_id: str, cas_id: int, room_id: str) -> None:
    """Помечает сообщение доставленным и запоминает активную комнату клиента.

    Привязка casId -> roomId нужна обратному каналу: Битрикс присылает ответ
    оператора, а отправлять его надо в конкретную комнату ДомКлик.
    """
    state = read_bridge()
    seen: list[str] = list(state.get("seen") or [])
    if message_id not in seen:
        seen.append(message_id)
    state["seen"] = seen[-SEEN_LIMIT:]

    rooms: dict[str, Any] = dict(state.get("rooms") or {})
    rooms[str(cas_id)] = {"room_id": room_id, "last_message_at": _now()}
    state["rooms"] = rooms

    write_json(bridge_path(), state)


def room_for(cas_id: int) -> str:
    """Комната, куда отправлять ответ оператора этому клиенту."""
    rooms = read_bridge().get("rooms") or {}
    entry = rooms.get(str(cas_id)) or {}
    return str(entry.get("room_id") or "")


def cas_for_room(room_id: str) -> int | None:
    for cas_id, entry in (read_bridge().get("rooms") or {}).items():
        if (entry or {}).get("room_id") == room_id:
            return int(cas_id)
    return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
