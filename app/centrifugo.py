"""Слушатель Centrifugo — «уши» моста.

Держит постоянное соединение с push-сервером ДомКлик и отдаёт наверх каждое
новое сообщение. Опрашивать REST по таймеру не нужно: у ДомКлик уже есть
готовая доставка, которой пользуется их же браузерный виджет.

Протокол v2, сервер Centrifugo 4.x. Токен не передаём: аутентификация идёт по
кукам сессии через connect-прокси.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

import websockets
from websockets.exceptions import WebSocketException

log = logging.getLogger("bridge.centrifugo")

WS_URL = "wss://soba.domclick.ru/connection/websocket?cf_protocol_version=v2"
ORIGIN = "https://homeland-projects.domclick.ru"

# Так представляется браузерный виджет — не отсвечиваем без нужды.
CLIENT_NAME = "homeland-projects.domclick.ru"
CLIENT_VERSION = "2.1.3"

# Если сервер не пришлёт свой интервал, шлём «я на связи» с этим шагом.
DEFAULT_ONLINE_INTERVAL = 300

MAX_BACKOFF = 60.0

PushHandler = Callable[[dict[str, Any]], Awaitable[None]]


class CentrifugoListener:
    def __init__(self, cookies: dict[str, str], cas_id: int, on_push: PushHandler) -> None:
        self._cookies = cookies
        self._cas_id = cas_id
        self._on_push = on_push
        self._next_id = 0
        self.connected = False
        self.last_error = ""

    @property
    def channels(self) -> list[str]:
        # Персональный канал: решётка означает, что подписаться может только этот
        # пользователь. Все чатовые события приходят сюда.
        return [f"auth-chat:individual#{self._cas_id}"]

    async def run(self, stop: asyncio.Event) -> None:
        """Бесконечный цикл с переподключением."""
        backoff = 1.0
        while not stop.is_set():
            try:
                await self._session(stop)
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except (WebSocketException, OSError) as error:
                self.connected = False
                if stop.is_set():
                    break  # это мы сами закрыли сокет при остановке
                self.last_error = f"{type(error).__name__}: {error}"
                log.warning("Соединение потеряно (%s), повтор через %.0f с", self.last_error, backoff)
            except Exception as error:  # noqa: BLE001 — слушатель не имеет права умирать
                self.connected = False
                self.last_error = f"{type(error).__name__}: {error}"
                log.exception("Неожиданная ошибка слушателя, повтор через %.0f с", backoff)

            if stop.is_set():
                break
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, MAX_BACKOFF)

        self.connected = False
        log.info("Слушатель остановлен")

    async def _session(self, stop: asyncio.Event) -> None:
        headers = {
            "Cookie": "; ".join(f"{name}={value}" for name, value in self._cookies.items()),
            "Origin": ORIGIN,
        }
        async with websockets.connect(
            WS_URL,
            extra_headers=headers,
            ping_interval=None,  # пинги у Centrifugo свои, поверх протокола
            max_size=4 * 1024 * 1024,
        ) as socket:
            await self._send(socket, {"connect": {"name": CLIENT_NAME, "version": CLIENT_VERSION}})
            for channel in self.channels:
                await self._send(socket, {"subscribe": {"channel": channel, "flag": 1}})

            self.connected = True
            self.last_error = ""
            log.info("Подключён к Centrifugo, каналы: %s", ", ".join(self.channels))

            keepalive = asyncio.create_task(self._keepalive(socket, stop))
            # recv() блокируется намертво и флагом его не прервать, поэтому по
            # сигналу остановки закрываем сокет — чтение выйдет само.
            closer = asyncio.create_task(self._close_on_stop(socket, stop))
            try:
                await self._read_loop(socket)
            finally:
                keepalive.cancel()
                closer.cancel()
                self.connected = False

    async def _close_on_stop(self, socket, stop: asyncio.Event) -> None:
        await stop.wait()
        await socket.close()

    async def _read_loop(self, socket) -> None:
        async for raw in socket:
            # В одном кадре может приехать несколько команд, разделённых переводом строки.
            for line in str(raw).split("\n"):
                line = line.strip()
                if not line:
                    continue
                try:
                    frame = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("Нечитаемый кадр: %r", line[:200])
                    continue

                if frame == {}:
                    await socket.send("{}")  # пинг-понг
                    continue

                push = frame.get("push")
                if push:
                    await self._dispatch(push)

    async def _dispatch(self, push: dict[str, Any]) -> None:
        try:
            await self._on_push(push)
        except Exception:  # noqa: BLE001 — одно плохое сообщение не рвёт соединение
            log.exception("Обработчик пуша упал, продолжаем слушать")

    async def _keepalive(self, socket, stop: asyncio.Event) -> None:
        """ChatSetOnline: без него подрядчик числится офлайн в глазах клиентов."""
        interval = float(DEFAULT_ONLINE_INTERVAL)
        while not stop.is_set():
            try:
                await self._send(socket, {"rpc": {"method": "ChatSetOnline"}})
            except (WebSocketException, OSError):
                return
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                continue

    async def _send(self, socket, command: dict[str, Any]) -> None:
        self._next_id += 1
        await socket.send(json.dumps({**command, "id": self._next_id}))


def parse_message_push(push: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Достаёт (сообщение, комната) из пуша нового сообщения.

    Интересует только action=add: update — это отметки о прочтении и правки
    комнаты, их пропускаем.
    """
    data = ((push or {}).get("pub") or {}).get("data") or {}
    if data.get("action") != "add":
        return None
    inner = data.get("data") or {}
    message = inner.get("message")
    if not isinstance(message, dict):
        return None
    room = inner.get("room") if isinstance(inner.get("room"), dict) else {}
    return message, room
