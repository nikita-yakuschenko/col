"""Клиент REST API Битрикс24 и регистрация коннектора.

Токены обновляем по факту ошибки, а не по расписанию: документация прямо
предупреждает, что превентивное обновление создаёт лишнюю нагрузку на сервер
авторизации и приложение за это могут заблокировать.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

import httpx

from app.config import settings
from app.storage import read_json, write_json

log = logging.getLogger("bridge.b24")

OAUTH_URL = "https://oauth.bitrix24.tech/oauth/token/"

# Ошибки, после которых имеет смысл обновить токен и повторить вызов.
_STALE_TOKEN_ERRORS = {"expired_token", "invalid_token"}


class BitrixError(RuntimeError):
    def __init__(self, code: str, description: str = "") -> None:
        super().__init__(f"{code}: {description}" if description else code)
        self.code = code
        self.description = description


class BitrixClient:
    """Вызовы REST от имени приложения с однократным обновлением токена."""

    def __init__(self, timeout: float = 30.0) -> None:
        self._timeout = timeout

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        tokens = read_json(settings.tokens_path, default={})
        access_token = tokens.get("access_token")
        if not access_token:
            raise BitrixError("NOT_INSTALLED", "Приложение не установлено, токенов нет")

        try:
            return await self._request(method, params or {}, access_token)
        except BitrixError as error:
            if error.code not in _STALE_TOKEN_ERRORS:
                raise
            log.info("Токен протух на %s, обновляю", method)
            access_token = await self.refresh()
            return await self._request(method, params or {}, access_token)

    async def _request(self, method: str, params: dict[str, Any], access_token: str) -> Any:
        url = f"{self._rest_base()}{method}"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(url, json={**params, "auth": access_token})

        payload = _as_json(response)
        if "error" in payload:
            raise BitrixError(str(payload["error"]), str(payload.get("error_description", "")))
        return payload.get("result")

    async def refresh(self) -> str:
        """Меняет refresh_token на новую пару токенов и сохраняет её."""
        tokens = read_json(settings.tokens_path, default={})
        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            raise BitrixError("NO_REFRESH_TOKEN", "Нечем обновлять, нужна переустановка приложения")

        # Строго POST с телом: при GET секреты уезжают в query, а httpx пишет
        # полный URL в лог — client_secret и refresh_token оказываются в логах.
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                OAUTH_URL,
                data={
                    "grant_type": "refresh_token",
                    "client_id": settings.b24_client_id,
                    "client_secret": settings.b24_client_secret,
                    "refresh_token": refresh_token,
                },
            )

        payload = _as_json(response)
        if "error" in payload:
            raise BitrixError(str(payload["error"]), str(payload.get("error_description", "")))

        # application_token сервер авторизации не возвращает — сохраняем прежний,
        # иначе проверка входящих событий перестанет проходить.
        tokens.update(
            {
                "access_token": payload["access_token"],
                "refresh_token": payload["refresh_token"],
                "expires_in": payload.get("expires_in", ""),
                "client_endpoint": payload.get("client_endpoint", ""),
            }
        )
        write_json(settings.tokens_path, tokens)
        log.info("Токены обновлены")
        return payload["access_token"]

    def _rest_base(self) -> str:
        tokens = read_json(settings.tokens_path, default={})
        endpoint = tokens.get("client_endpoint")
        if endpoint:
            return endpoint if endpoint.endswith("/") else f"{endpoint}/"
        return f"{settings.b24_portal}/rest/"


# --- регистрация коннектора -------------------------------------------------

def _icon(color: str, glyph_fill: str = "#ffffff") -> dict[str, str]:
    """Иконка коннектора: SVG прямо в data-URI, внешние картинки не поддерживаются."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
        f'<path fill="{glyph_fill}" d="M12 3 3 10.2V21h6.2v-6.3h5.6V21H21V10.2z"/>'
        "</svg>"
    )
    return {
        "DATA_IMAGE": "data:image/svg+xml," + quote(svg, safe=""),
        "COLOR": color,
        "SIZE": "80%",
        "POSITION": "center",
    }


async def register_connector(client: BitrixClient) -> dict[str, Any]:
    """Регистрирует коннектор и подписывается на ответы оператора.

    Метод идемпотентен: повторный вызов с тем же ID обновляет существующий
    коннектор, поэтому его безопасно дёргать при каждой установке.
    """
    result: dict[str, Any] = {}

    result["register"] = await client.call(
        "imconnector.register",
        {
            "ID": settings.connector_id,
            "NAME": "ДомКлик",
            "ICON": _icon("#1ab248"),
            "ICON_DISABLED": _icon("#99adb3"),
            "PLACEMENT_HANDLER": f"{settings.public_url}/b24/handler",
            # false — группировка по user.id: все обращения одного человека
            # попадают в один диалог, история не рвётся.
            "CHAT_GROUP": False,
        },
    )

    for event in ("ONIMCONNECTORMESSAGEADD",):
        try:
            result[event] = await client.call(
                "event.bind",
                {"event": event, "handler": f"{settings.public_url}/b24/handler"},
            )
        except BitrixError as error:
            # Повторная подписка на уже привязанное событие — не повод падать.
            if error.code == "ERROR_EVENT_BINDING_EXISTS":
                result[event] = "already bound"
            else:
                raise

    return result


def _as_json(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        raise BitrixError("BAD_RESPONSE", f"HTTP {response.status_code}: {response.text[:200]}")
    return payload if isinstance(payload, dict) else {"result": payload}
