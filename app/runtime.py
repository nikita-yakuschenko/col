"""Запуск и остановка живой части сервиса.

Собирает вместе клиент ДомКлик, слушатель Centrifugo и мост, следит за тем,
чтобы падение одного не уронило веб-приложение, и умеет перезапускаться после
загрузки свежих кук.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app import state
from app.bitrix import BitrixClient
from app.bridge import Bridge
from app.centrifugo import CentrifugoListener
from app.domclick import DomClickClient, SessionExpired

log = logging.getLogger("bridge.runtime")


class Runtime:
    """Живая часть: одно соединение с ДомКлик на весь процесс."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._listener: CentrifugoListener | None = None
        self._bridge: Bridge | None = None
        self.dc: DomClickClient | None = None
        self.status = "не запущен"
        self.cas_id: int | None = None

    @property
    def bridge(self) -> Bridge | None:
        return self._bridge

    def snapshot(self) -> dict[str, Any]:
        """Состояние для /health — чтобы не лазить в логи контейнера."""
        return {
            "status": self.status,
            "cas_id": self.cas_id,
            "connected": bool(self._listener and self._listener.connected),
            "forwarded": self._bridge.forwarded if self._bridge else 0,
            "skipped": self._bridge.skipped if self._bridge else 0,
            "last_error": (self._listener.last_error if self._listener else "")
            or (self._bridge.last_error if self._bridge else ""),
        }

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop = asyncio.Event()
        # Ставим статус сразу: задача стартует только на следующем витке цикла,
        # а /health могут спросить раньше — и «остановлен» будет враньём.
        self.status = "запускается"
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=15)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        self._task = None
        self.status = "остановлен"

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    async def _run(self) -> None:
        try:
            self.dc = DomClickClient.from_storage_state(state.cookies_path())
        except SessionExpired as error:
            self.status = f"нет сессии ДомКлик: {error}"
            log.warning(self.status)
            return

        try:
            self.cas_id = await self.dc.whoami()
        except Exception as error:  # noqa: BLE001
            self.status = f"вход в ДомКлик не удался: {error}"
            log.warning(self.status)
            return

        # Отсечка: всё, что было до первого запуска, в открытую линию не едет.
        state.start_watching(int(time.time() * 1000))

        self._bridge = Bridge(self.dc, BitrixClient(), self.cas_id)
        self._listener = CentrifugoListener(self.dc.cookies, self.cas_id, self._bridge.handle_push)
        self.status = "работает"
        log.info("Мост запущен, casId=%s", self.cas_id)

        try:
            await self._listener.run(self._stop)
        finally:
            self.status = "остановлен"


runtime = Runtime()
