"""Клиент чата подрядчика ДомКлик.

Официального API нет, всё восстановлено перехватом трафика — подробности и
предостережения в docs/domclick-chat-protocol.md.
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("bridge.domclick")

CHAT_API = "https://ipoteka.domclick.ru/chat/api/v3"
AUTH_ME = "https://api.domclick.ru/auth/me"
CABINET = "https://homeland-projects.domclick.ru"

# Системный отправитель ДомКлик: служебные сообщения вроде «Чат создан».
SYSTEM_CAS_ID = -900

# Тег, по которому проектные комнаты отличаются от ипотечных и по объявлениям.
PROJECT_TAG = "suburban_developer"

# Тег сделки. В таких комнатах сидят покупатели, представители застройщика
# и ИИ-помощник — это не обращения к подрядчику, нам они не нужны.
DEAL_TAG = "deal"

# В обращении к подрядчику участвуют только клиент и наши учётки. Всё
# остальное (BOT — ИИ-помощник, BUYER — покупатель, AGENT — представитель
# застройщика) означает, что комната из другого сценария.
ALLOWED_ROLES = {"USER", "CONTRACTOR"}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Origin": CABINET,
    "Referer": f"{CABINET}/",
    # Без него любой запрос отвечает 400: "'X-Service' header is mandatory".
    "X-Service": "web/widget/1.0.1/build-number-null",
}


class SessionExpired(RuntimeError):
    """Куки протухли — нужен повторный вход с подтверждением по SMS."""


class DomClickClient:
    def __init__(self, cookies: dict[str, str], timeout: float = 30.0) -> None:
        self._cookies = cookies
        self._timeout = timeout

    @property
    def cookies(self) -> dict[str, str]:
        """Те же куки нужны слушателю Centrifugo для рукопожатия."""
        return dict(self._cookies)

    @classmethod
    def from_storage_state(cls, path: Path) -> "DomClickClient":
        """Читает куки из storage-state.json, снятого Playwright."""
        if not path.exists():
            raise SessionExpired(f"Нет файла с куками: {path}")
        state = json.loads(path.read_text(encoding="utf-8"))
        cookies = {
            cookie["name"]: cookie["value"]
            for cookie in state.get("cookies", [])
            if "domclick.ru" in cookie.get("domain", "")
        }
        if not cookies:
            raise SessionExpired("В storage-state.json нет кук домена domclick.ru")
        return cls(cookies)

    async def whoami(self) -> int:
        """casId нашего аккаунта подрядчика. Нужен и для подписки, и для фильтра эха."""
        payload = await self._get(AUTH_ME)
        cas_id = payload.get("casId")
        if not isinstance(cas_id, int):
            raise SessionExpired("auth/me не вернул casId — похоже, сессия недействительна")
        return cas_id

    async def list_rooms(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        payload = await self._get(
            f"{CHAT_API}/rooms", params={"list": "main", "limit": limit, "offset": offset}
        )
        return payload.get("data") or []

    async def all_project_rooms(self, page_size: int = 50, max_pages: int = 40) -> list[dict[str, Any]]:
        """Все проектные комнаты с постраничным обходом.

        `/rooms` отдаёт вперемешку ипотечные чаты, чаты по объявлениям и наши
        проектные, поэтому фильтруем по тегу подрядчика.
        """
        rooms: list[dict[str, Any]] = []
        for page in range(max_pages):
            batch = await self.list_rooms(limit=page_size, offset=page * page_size)
            rooms.extend(room for room in batch if is_project_room(room))
            if len(batch) < page_size:
                break
        else:
            log.warning("Обход комнат прерван на %s страницах — возможно, есть ещё", max_pages)
        return rooms

    async def get_messages(self, room_id: str, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        payload = await self._get(
            f"{CHAT_API}/rooms/{room_id}/messages",
            params={"limit": limit, "offset": offset, "mode": "reply_on", "mentions_on": "true"},
        )
        return payload.get("data") or []

    async def send_message(self, room_id: str, text: str, message_uuid: str | None = None) -> dict[str, Any]:
        """Отправляет сообщение в комнату.

        uuid генерирует клиент — это ключ идемпотентности, поэтому повторная
        отправка с тем же значением не создаёт дубликат.
        """
        body = {
            "type": "message",
            "message": text,
            "uuid": message_uuid or str(uuid.uuid4()),
        }
        async with self._client() as client:
            response = await client.post(
                f"{CHAT_API}/rooms/{room_id}/messages",
                params={"bot_buttons": "true"},
                json=body,
            )
        self._guard(response)
        if response.status_code not in (200, 201):
            raise RuntimeError(f"Отправка не удалась: HTTP {response.status_code} {response.text[:200]}")
        return body

    async def _get(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        async with self._client() as client:
            response = await client.get(url, params=params)
        self._guard(response)
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {"data": payload}

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers=_HEADERS,
            cookies=self._cookies,
            timeout=self._timeout,
            follow_redirects=False,
        )

    @staticmethod
    def _guard(response: httpx.Response) -> None:
        """Протухшая сессия проявляется редиректом на вход или 401/403."""
        if response.status_code in (401, 403) or 300 <= response.status_code < 400:
            raise SessionExpired(f"ДомКлик ответил {response.status_code} — сессия недействительна")


# --- разбор данных комнаты --------------------------------------------------

def room_tags(room: dict[str, Any]) -> dict[str, str]:
    return {tag.get("name", ""): str(tag.get("value", "")) for tag in room.get("tags") or []}


def is_project_room(room: dict[str, Any]) -> bool:
    """Комната-обращение к подрядчику, а не чат по сделке.

    Проверяем двумя независимыми способами: по тегам и по составу участников.
    Тега `suburban_developer` на боевых данных достаточно, но состав участников
    подстрахует, если ДомКлик начнёт расставлять теги иначе.
    """
    tags = room_tags(room)
    if PROJECT_TAG not in tags or DEAL_TAG in tags:
        return False
    return not foreign_roles(room)


def foreign_roles(room: dict[str, Any]) -> set[str]:
    """Роли участников, которых в обращении к подрядчику быть не должно."""
    found: set[str] = set()
    for member in room.get("members") or []:
        for role in member.get("chatRole") or []:
            if role not in ALLOWED_ROLES:
                found.add(role)
    return found


def client_cas_id(room: dict[str, Any]) -> int | None:
    """Идентификатор клиента каскадом: тег, потом участник, потом автор комнаты.

    На боевых данных тег `client_cas_id` есть примерно у половины комнат,
    поэтому одного источника недостаточно.
    """
    tags = room_tags(room)
    for candidate in (tags.get("client_cas_id"), tags.get("author_id")):
        if candidate and candidate.lstrip("-").isdigit():
            return int(candidate)

    member = client_member(room)
    if member and isinstance(member.get("casId"), int):
        return member["casId"]
    return None


def client_member(room: dict[str, Any]) -> dict[str, Any] | None:
    """Участник-клиент.

    Ориентируемся строго на chatRole: поле role перевёрнуто и у клиента там
    стоит AGENT, а у подрядчика CUSTOMER — на нём легко ошибиться.
    """
    for member in room.get("members") or []:
        if "USER" in (member.get("chatRole") or []):
            return member
    return None


def split_name(display_name: str) -> tuple[str, str]:
    """«Якущенко Никита Сергеевич» -> («Никита», «Якущенко»).

    Битрикс принимает имя и фамилию отдельно и не любит отчество в этих полях.
    """
    parts = [part for part in (display_name or "").split() if part]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[1], parts[0]
