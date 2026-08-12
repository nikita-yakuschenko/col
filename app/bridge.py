"""Мост: сообщение из ДомКлик -> Открытая линия Битрикс24.

Здесь живут четыре решения, каждое из которых защищает от конкретной поломки:
отсечка по времени, фильтр эха, дедупликация и отбор проектных комнат.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app import domclick, state
from app.bitrix import BitrixClient
from app.config import settings

log = logging.getLogger("bridge.pipe")

# Название проекта структурно нигде не лежит — только в тексте пресета,
# которым ДомКлик открывает чат по кнопке «получить смету».
PROJECT_RE = re.compile(r"по проекту дома\s+[«\"]([^»\"]+)[»\"]", re.IGNORECASE)

# Битрикс присылает ответ оператора с BB-кодами и подписью автора.
BB_TAGS = re.compile(r"\[/?[a-zA-Z]+(?:=[^\]]*)?\]")


class Bridge:
    def __init__(self, dc: domclick.DomClickClient, b24: BitrixClient, own_cas_id: int) -> None:
        self._dc = dc
        self._b24 = b24
        self._own_cas_id = own_cas_id
        self.forwarded = 0
        self.skipped = 0
        self.last_error = ""

    async def handle_push(self, push: dict[str, Any]) -> None:
        from app.centrifugo import parse_message_push

        parsed = parse_message_push(push)
        if not parsed:
            return
        message, room = parsed
        await self.handle_message(message, room)

    async def handle_message(self, message: dict[str, Any], room: dict[str, Any]) -> None:
        reason = self._skip_reason(message, room)
        if reason:
            self.skipped += 1
            log.debug("Пропуск (%s): %s", reason, message.get("_id"))
            return

        line = state.read_line()
        if not line.get("active") or not line.get("line"):
            self.skipped += 1
            log.warning("Коннектор не подключён к линии — сообщение %s не передано", message.get("_id"))
            return

        cas_id = domclick.client_cas_id(room) or message.get("fromCasId")
        payload = self._to_bitrix(message, room, int(cas_id))

        try:
            await self._b24.call(
                "imconnector.send.messages",
                {
                    "CONNECTOR": settings.connector_id,
                    "LINE": int(line["line"]),
                    "MESSAGES": [payload],
                },
            )
        except Exception as error:  # noqa: BLE001 — сообщение важнее, чем падение задачи
            self.last_error = f"{type(error).__name__}: {error}"
            log.exception("Не удалось передать сообщение %s в Битрикс", message.get("_id"))
            return

        state.remember(str(message.get("_id")), int(cas_id), str(message.get("roomId") or ""))
        self.forwarded += 1
        log.info("Передано в линию %s: %s от %s", line["line"], message.get("_id"), cas_id)

    def _skip_reason(self, message: dict[str, Any], room: dict[str, Any]) -> str:
        message_id = str(message.get("_id") or "")
        if not message_id:
            return "нет идентификатора"

        if message.get("type") != "message":
            return "служебное сообщение"

        sender = message.get("fromCasId")
        # Эхо: наш собственный ответ возвращается тем же каналом. Без этого
        # фильтра он ушёл бы в Битрикс как реплика клиента и закрутил цикл.
        if sender == self._own_cas_id:
            return "эхо нашего сообщения"
        if sender == domclick.SYSTEM_CAS_ID:
            return "системное сообщение ДомКлик"

        # Канал recoverable: после переподключения сервер досылает пропущенное.
        if state.is_seen(message_id):
            return "уже передано"

        # Отсечка: старые переписки в линию не переносим.
        cutoff = state.cutoff_ms()
        if cutoff and int(message.get("time") or 0) <= cutoff:
            return "старее отсечки"

        # Комнаты бывают ипотечные и по объявлениям — они не наши.
        if room and not domclick.is_project_room(room):
            return "не проектная комната"

        return ""

    def _to_bitrix(self, message: dict[str, Any], room: dict[str, Any], cas_id: int) -> dict[str, Any]:
        member = domclick.client_member(room) or {}
        first_name, last_name = domclick.split_name(member.get("displayName", ""))
        text = str(message.get("message") or "")

        chat_name = "ДомКлик"
        project = PROJECT_RE.search(text)
        if project:
            chat_name = f"ДомКлик · {project.group(1)}"

        return {
            "user": {
                # casId сквозной по экосистеме Сбера и не меняется — именно по
                # нему Битрикс склеивает историю в один диалог.
                "id": str(cas_id),
                "name": first_name,
                "last_name": last_name,
            },
            "message": {
                "id": str(message.get("_id")),
                "date": int(int(message.get("time") or 0) / 1000),
                "text": text,
            },
            "chat": {
                "id": str(message.get("roomId") or ""),
                "name": chat_name,
            },
        }


def strip_operator_markup(text: str) -> str:
    """Чистит текст ответа оператора перед отправкой в ДомКлик.

    Битрикс подставляет подпись и BB-коды: «[b]Имя:[/b] [br]текст». В чате
    ДомКлик разметка не поддерживается и приехала бы как мусор.
    """
    text = text.replace("[br]", "\n")
    text = BB_TAGS.sub("", text)
    # После вырезания тегов остаются висячие пробелы по краям строк.
    return "\n".join(line.strip() for line in text.splitlines()).strip()
