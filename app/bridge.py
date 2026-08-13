"""Мост: сообщение из ДомКлик -> Открытая линия Битрикс24.

Здесь живут четыре решения, каждое из которых защищает от конкретной поломки:
отсечка по времени, фильтр эха, дедупликация и отбор проектных комнат.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import time
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

# Подпись оператора в начале сообщения: «[b]Имя Фамилия:[/b] [br]текст».
SIGNATURE = re.compile(r"^\s*\[b\](?P<name>[^\[]+?):?\[/b\]\s*(?:\[br\])?\s*", re.IGNORECASE)

# Чем выделять имя оператора. Проверено вживую 13.08.2026: чат ДомКлик понимает
# markdown — `**` даёт жирный, `*` курсив, работает и HTML `<b>`.
NAME_WRAP_BEFORE = "**"
NAME_WRAP_AFTER = "**"


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
        # Сообщение без текста от клиента — почти наверняка вложение. Формат
        # таких сообщений мы ещё не видели, поэтому пишем его целиком.
        if message.get("type") == "message" and not str(message.get("message") or "").strip():
            log.warning(
                "Сообщение без текста, вероятно вложение: %s",
                json.dumps(message, ensure_ascii=False)[:2000],
            )

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

        # fileSet — сообщение с вложением, оно нам тоже нужно.
        if message.get("type") not in ("message", "fileSet"):
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

        payload_message: dict[str, Any] = {
            "id": str(message.get("_id")),
            "date": int(int(message.get("time") or 0) / 1000),
            "text": text,
        }

        files = _publish_files(message)
        if files:
            payload_message["files"] = files
            if not text:
                # Битриксу нужен хотя бы один из двух: текст или файлы. Текст
                # оставляем осмысленным, иначе в чате будет пустой пузырь.
                payload_message["text"] = "Вложение"

        return {
            "user": {
                # casId сквозной по экосистеме Сбера и не меняется — именно по
                # нему Битрикс склеивает историю в один диалог.
                "id": str(cas_id),
                "name": first_name,
                "last_name": last_name,
            },
            "message": payload_message,
            "chat": {
                "id": str(message.get("roomId") or ""),
                "name": chat_name,
            },
        }


def _publish_files(message: dict[str, Any]) -> list[dict[str, str]]:
    """Готовит вложения к передаче в Битрикс.

    Ссылки ДомКлик открываются только с куками сессии, а Битрикс качает файлы
    анонимно и получил бы 401. Поэтому на каждый файл заводим одноразовый адрес
    на нашем сервисе, а тот уже скачивает оригинал с куками.
    """
    published: list[dict[str, str]] = []
    for item in message.get("files") or []:
        source = str(item.get("url") or "")
        name = str(item.get("name") or item.get("filename") or "file")
        if not source:
            continue
        token = secrets.token_urlsafe(24)
        state.remember_file(token, source, name)
        published.append({"url": f"{settings.public_url}/f/{token}", "name": name})
    return published


async def deliver_operator_reply(
    dc: domclick.DomClickClient,
    b24: BitrixClient,
    data: dict[str, Any],
) -> int:
    """Обратный канал: ответ оператора из Битрикса -> в чат ДомКлик.

    Данные приходят из события ONIMCONNECTORMESSAGEADD.
    """
    line = data.get("LINE") or (state.read_line().get("line") or 0)
    messages = _as_list(data.get("MESSAGES"))
    delivered: list[dict[str, Any]] = []

    for item in messages:
        im = item.get("im") or {}
        text = strip_operator_markup(str((item.get("message") or {}).get("text") or ""))
        room_id = str((item.get("chat") or {}).get("id") or "")

        if not room_id:
            # Битрикс группирует диалоги по user.id, поэтому chat.id иногда пуст —
            # выручает привязка, накопленная на входящих сообщениях.
            cas_id = (item.get("message") or {}).get("user_id")
            room_id = state.room_for(int(cas_id)) if cas_id else ""

        attachments = _operator_files(item)
        if attachments and room_id:
            delivered_files = await _send_operator_files(dc, room_id, attachments)
            if delivered_files:
                delivered.append(
                    {
                        "im": {
                            "chat_id": _as_int(im.get("chat_id")),
                            "message_id": _as_int(im.get("message_id")),
                        },
                        "message": {"id": [delivered_files["uuid"]], "date": int(time.time())},
                        "chat": {"id": room_id},
                    }
                )
            if not text:
                continue

        if not text or not room_id:
            # Скорее всего оператор приложил файл: события с вложениями Битрикс
            # шлёт без текста. Пишем payload целиком — по нему разберём формат
            # и научимся передавать вложения.
            log.warning(
                "Ответ оператора не отправлен: текст=%r комната=%r payload=%s",
                text[:40],
                room_id,
                json.dumps(item, ensure_ascii=False)[:2000],
            )
            continue

        try:
            sent = await dc.send_message(room_id, text)
        except Exception:  # noqa: BLE001 — один неудачный ответ не рвёт обработку остальных
            log.exception("Не удалось отправить ответ оператора в комнату %s", room_id)
            continue

        log.info("Ответ оператора отправлен в комнату %s", room_id)
        delivered.append(
            {
                "im": {"chat_id": _as_int(im.get("chat_id")), "message_id": _as_int(im.get("message_id"))},
                "message": {"id": [sent["uuid"]], "date": int(time.time())},
                "chat": {"id": room_id},
            }
        )

    if delivered and line:
        try:
            await b24.call(
                "imconnector.send.status.delivery",
                {"CONNECTOR": settings.connector_id, "LINE": int(line), "MESSAGES": delivered},
            )
        except Exception:  # noqa: BLE001 — галочка о доставке не важнее самой доставки
            log.exception("Подтверждение доставки не прошло")

    return len(delivered)


def _operator_files(item: dict[str, Any]) -> list[dict[str, str]]:
    """Вытаскивает вложения оператора из события Битрикса.

    Формат в документации не описан, поэтому принимаем несколько вариантов
    названий полей — какое из них живое, покажет боевое событие.
    """
    raw = (item.get("message") or {}).get("files")
    found: list[dict[str, str]] = []
    for entry in _as_list(raw):
        link = str(
            entry.get("link") or entry.get("url") or entry.get("urlDownload") or entry.get("src") or ""
        )
        name = str(entry.get("name") or entry.get("fileName") or entry.get("filename") or "file")
        if link:
            found.append({"link": link, "name": name})
    return found


async def _send_operator_files(
    dc: domclick.DomClickClient, room_id: str, attachments: list[dict[str, str]]
) -> dict[str, Any] | None:
    """Скачивает файлы из Битрикса и отправляет их в чат ДомКлик."""
    uploaded: list[dict[str, Any]] = []
    for attachment in attachments:
        try:
            content, content_type = await _download_from_bitrix(attachment["link"])
            uploaded.append(
                await dc.upload_file(room_id, content, attachment["name"], content_type)
            )
        except Exception:  # noqa: BLE001 — один файл не должен рушить остальные
            log.exception("Не удалось перенести вложение %s", attachment["name"])

    if not uploaded:
        return None
    try:
        return await dc.send_files(room_id, uploaded)
    except Exception:  # noqa: BLE001
        log.exception("Файлы загружены, но сообщение с ними не отправилось")
        return None


async def _download_from_bitrix(link: str) -> tuple[bytes, str]:
    """Качает файл из Битрикса.

    Ссылки на файлы открытых линий бывают и публичными, и требующими токен,
    поэтому при отказе повторяем с авторизацией приложения.
    """
    import httpx

    from app.storage import read_json

    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        response = await client.get(link)
        if response.status_code in (401, 403):
            token = (read_json(settings.tokens_path, default={}) or {}).get("access_token", "")
            response = await client.get(link, params={"auth": token})
    response.raise_for_status()
    return response.content, response.headers.get("content-type", "application/octet-stream")


def parse_bracketed(form: dict[str, str]) -> dict[str, Any]:
    """Разбирает плоские ключи вида data[MESSAGES][0][im][chat_id] в дерево.

    Битрикс шлёт события формой, а не JSON, поэтому вложенность закодирована
    в именах полей.
    """
    root: dict[str, Any] = {}
    for key, value in form.items():
        parts = re.findall(r"[^\[\]]+", key)
        node = root
        for part in parts[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                node[part] = nxt
            node = nxt
        node[parts[-1]] = value
    return root


def _as_list(value: Any) -> list[dict[str, Any]]:
    """Массивы в форме приезжают словарём с числовыми ключами."""
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        keys = sorted(value.keys(), key=lambda k: int(k) if str(k).isdigit() else 0)
        return [value[k] for k in keys if isinstance(value[k], dict)]
    return []


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def strip_operator_markup(text: str) -> str:
    """Готовит ответ оператора к отправке в ДомКлик.

    Битрикс подставляет подпись и BB-коды: «[b]Имя:[/b] [br]текст». Разметку
    убираем, а подпись отделяем от текста пустой строкой — так в чате клиента
    видно, где кончается имя и начинается ответ.
    """
    signature = SIGNATURE.match(text)
    name = ""
    if signature:
        name = signature.group("name").strip()
        text = text[signature.end():]

    text = text.replace("[br]", "\n")
    text = BB_TAGS.sub("", text)
    # После вырезания тегов остаются висячие пробелы по краям строк.
    body = "\n".join(line.strip() for line in text.splitlines()).strip()

    if not name:
        return body
    return f"{NAME_WRAP_BEFORE}{name}{NAME_WRAP_AFTER}\n\n{body}".strip()
