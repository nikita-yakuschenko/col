"""Мост «ДомКлик → Открытые линии Битрикс24».

Пока скелет: поднимает публичные эндпоинты, которые нужны Битриксу для установки
локального приложения, и сохраняет OAuth-токены. Логика чата подключается следующим
шагом — см. docs/domclick-chat-protocol.md.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.config import settings
from app.storage import read_json, write_json

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("bridge")


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    log.info("Старт. Данные: %s", settings.data_dir)
    if settings.missing:
        log.warning("Не заданы переменные окружения: %s", ", ".join(settings.missing))
    yield
    log.info("Остановка")


app = FastAPI(title="ДомКлик → Открытые линии", lifespan=lifespan, docs_url=None, redoc_url=None)


@app.get("/health")
async def health() -> JSONResponse:
    """Проверка живости: используется Dokploy и мной снаружи для контроля TLS."""
    tokens = read_json(settings.tokens_path, default={})
    return JSONResponse(
        {
            "status": "ok",
            "time": datetime.now(timezone.utc).isoformat(),
            "b24_configured": settings.is_b24_configured,
            "b24_installed": bool(tokens.get("access_token")),
            "connector_id": settings.connector_id,
        }
    )


@app.api_route("/b24/install", methods=["GET", "POST"])
async def b24_install(request: Request) -> HTMLResponse:
    """Обработчик первоначальной установки локального приложения.

    Битрикс присылает сюда токены формой и ждёт страницу, которая вызовет
    BX24.installFinish(). Без этого вызова приложение остаётся в состоянии
    «не установлено» и события в него не идут.
    """
    payload = dict(await request.form()) if request.method == "POST" else dict(request.query_params)
    _remember_auth(payload, source="install")
    log.info("Установка приложения: member_id=%s", payload.get("member_id", "?"))
    return HTMLResponse(_INSTALL_PAGE)


@app.api_route("/b24/handler", methods=["GET", "POST"])
async def b24_handler(request: Request):
    """Единая точка для событий и страницы настроек коннектора.

    Битрикс дёргает этот URL в двух разных сценариях, различать их приходится по
    содержимому формы: `event` — событие, `PLACEMENT` — открытие слайдера настроек.
    """
    payload = dict(await request.form()) if request.method == "POST" else dict(request.query_params)

    placement = payload.get("PLACEMENT")
    if placement:
        log.info("Открыт слайдер настроек: PLACEMENT=%s", placement)
        _remember_auth(payload, source="placement")
        return HTMLResponse(_SETTINGS_PAGE)

    event = payload.get("event")
    if event:
        # TODO: ONIMCONNECTORMESSAGEADD -> отправка ответа оператора в ДомКлик.
        log.info("Событие %s", event)
        return JSONResponse({"ok": True})

    log.info("Неопознанный запрос к обработчику: ключи=%s", sorted(payload))
    return JSONResponse({"ok": True})


def _remember_auth(payload: dict, *, source: str) -> None:
    """Сохраняет токены, если Битрикс их прислал.

    В запросах к обработчикам авторизация приходит плоскими полями AUTH_ID/REFRESH_ID,
    а в событиях — вложенным объектом auth[...]. Здесь разбираем первый случай;
    второй появится вместе с обработкой событий.
    """
    access_token = payload.get("AUTH_ID")
    if not access_token:
        return

    tokens = {
        "access_token": access_token,
        "refresh_token": payload.get("REFRESH_ID", ""),
        "expires_in": payload.get("AUTH_EXPIRES", ""),
        "member_id": payload.get("member_id", ""),
        "domain": payload.get("DOMAIN", ""),
        "application_token": payload.get("APP_SID", ""),
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
    }
    write_json(settings.tokens_path, tokens)
    log.info("Токены сохранены (%s)", source)


_INSTALL_PAGE = """<!doctype html>
<html lang="ru">
<head><meta charset="utf-8"><title>Установка</title></head>
<body style="font-family:system-ui,sans-serif;padding:24px">
  <p>Приложение установлено.</p>
  <script src="//api.bitrix24.com/api/v1/"></script>
  <script>BX24.init(function () { BX24.installFinish(); });</script>
</body>
</html>
"""

_SETTINGS_PAGE = """<!doctype html>
<html lang="ru">
<head><meta charset="utf-8"><title>Настройки коннектора</title></head>
<body style="font-family:system-ui,sans-serif;padding:24px">
  <h3>ДомКлик</h3>
  <p>Коннектор подключён. Настройки появятся здесь на следующем шаге.</p>
  <script src="//api.bitrix24.com/api/v1/"></script>
  <script>BX24.init(function () { BX24.fitWindow(); });</script>
</body>
</html>
"""
