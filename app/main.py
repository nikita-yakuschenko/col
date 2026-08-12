"""Мост «ДомКлик → Открытые линии Битрикс24».

Публичные эндпоинты, которые нужны Битриксу: установка локального приложения,
приём событий и страница настроек коннектора. Логика чата подключается следующим
шагом — см. docs/domclick-chat-protocol.md.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.bitrix import BitrixClient, BitrixError, register_connector
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
    """Проверка живости: используется Dokploy и снаружи для контроля деплоя."""
    tokens = read_json(settings.tokens_path, default={})
    return JSONResponse(
        {
            "status": "ok",
            "time": datetime.now(timezone.utc).isoformat(),
            "b24_configured": settings.is_b24_configured,
            "b24_installed": bool(tokens.get("access_token")),
            "install_source": tokens.get("source", ""),
            "connector_id": settings.connector_id,
            "connector_registered": read_json(_register_report_path(), default={}),
        }
    )


def _register_report_path():
    return settings.data_dir / "connector_registration.json"


async def _register_connector_safely() -> None:
    """Регистрирует коннектор после установки.

    Ошибку сюда не выпускаем: если регистрация не удалась, установка всё равно
    должна завершиться, иначе приложение застрянет в подвешенном состоянии.
    Результат кладём в /health, чтобы было видно без доступа к логам.
    """
    report: dict[str, object] = {"at": datetime.now(timezone.utc).isoformat()}
    try:
        report["result"] = await register_connector(BitrixClient())
        report["ok"] = True
        log.info("Коннектор зарегистрирован: %s", report["result"])
    except BitrixError as error:
        report.update({"ok": False, "error": error.code, "description": error.description})
        log.error("Регистрация коннектора не удалась: %s", error)
    except Exception as error:  # noqa: BLE001 — установка важнее любой неожиданности
        report.update({"ok": False, "error": type(error).__name__, "description": str(error)})
        log.exception("Регистрация коннектора упала")
    write_json(_register_report_path(), report)


@app.api_route("/b24/install", methods=["GET", "POST"])
async def b24_install(request: Request) -> HTMLResponse:
    """Установка приложения с интерфейсом.

    Битрикс присылает токены формой и ждёт страницу, которая вызовет
    BX24.installFinish(). Без этого вызова приложение остаётся в состоянии
    «не установлено» и события в него не идут.
    """
    payload = await _payload(request)
    saved = _save_auth(payload, source="install")
    log.info("Установка через страницу: токены %s", "сохранены" if saved else "не пришли")
    if saved:
        await _register_connector_safely()
    return HTMLResponse(_INSTALL_PAGE)


@app.api_route("/b24/handler", methods=["GET", "POST"])
async def b24_handler(request: Request):
    """Единая точка для событий и страницы настроек коннектора.

    Битрикс дёргает этот URL в нескольких сценариях, различать их приходится по
    содержимому формы: PLACEMENT — слайдер настроек, event — событие.
    """
    payload = await _payload(request)

    if payload.get("PLACEMENT"):
        log.info("Слайдер настроек: PLACEMENT=%s", payload["PLACEMENT"])
        _save_auth(payload, source="placement")
        return HTMLResponse(_SETTINGS_PAGE)

    event = (payload.get("event") or "").upper()
    if not event:
        log.info("Запрос без события и встройки, ключи=%s", sorted(payload))
        return JSONResponse({"ok": True})

    # Приложение без интерфейса устанавливается именно так: событием на обработчик.
    if event == "ONAPPINSTALL":
        saved = _save_auth(payload, source="event")
        log.info("Установка событием ONAPPINSTALL: токены %s", "сохранены" if saved else "отклонены")
        if saved:
            await _register_connector_safely()
        return JSONResponse({"ok": saved})

    if event == "ONAPPUNINSTALL":
        if _verify_event(payload):
            write_json(settings.tokens_path, {})
            log.warning("Приложение удалено, токены очищены")
        return JSONResponse({"ok": True})

    if not _verify_event(payload):
        log.warning("Событие %s отклонено: неверный application_token", event)
        return JSONResponse({"ok": False, "error": "invalid token"}, status_code=403)

    # TODO: ONIMCONNECTORMESSAGEADD -> отправка ответа оператора в ДомКлик.
    log.info("Событие %s принято", event)
    return JSONResponse({"ok": True})


async def _payload(request: Request) -> dict[str, str]:
    if request.method == "POST":
        return {key: str(value) for key, value in (await request.form()).items()}
    return dict(request.query_params)


def _save_auth(payload: dict[str, str], *, source: str) -> bool:
    """Достаёт токены из запроса и сохраняет их.

    Битрикс присылает авторизацию в двух разных формах: плоскими полями
    AUTH_ID/REFRESH_ID — при установке через браузер и при открытии встройки,
    и вложенным объектом auth[...] — в событиях. Поддерживаем оба варианта.
    """
    access_token = payload.get("AUTH_ID") or payload.get("auth[access_token]")
    if not access_token:
        return False

    domain = payload.get("DOMAIN") or payload.get("auth[domain]", "")
    if domain and not _is_our_portal(domain):
        # Иначе кто угодно смог бы прислать ONAPPINSTALL и подменить токены:
        # адрес обработчика публично известен.
        log.warning("Запрос с чужого портала отклонён: %s", domain)
        return False

    write_json(
        settings.tokens_path,
        {
            "access_token": access_token,
            "refresh_token": payload.get("REFRESH_ID") or payload.get("auth[refresh_token]", ""),
            "expires_in": payload.get("AUTH_EXPIRES") or payload.get("auth[expires_in]", ""),
            "member_id": payload.get("member_id") or payload.get("auth[member_id]", ""),
            "domain": domain,
            "application_token": (
                payload.get("APP_SID") or payload.get("auth[application_token]", "")
            ),
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "source": source,
        },
    )
    log.info("Токены сохранены (%s), портал %s", source, domain or "?")
    return True


def _verify_event(payload: dict[str, str]) -> bool:
    """Сверяет application_token события с сохранённым при установке.

    Путь обработчика публичный, так что без этой проверки любой желающий мог бы
    слать нам события от имени Битрикса.
    """
    stored = read_json(settings.tokens_path, default={}).get("application_token")
    incoming = payload.get("auth[application_token]") or payload.get("application_token", "")
    return bool(stored) and stored == incoming


def _is_our_portal(domain: str) -> bool:
    expected = urlparse(settings.b24_portal).netloc or settings.b24_portal
    return domain.strip().lower() == expected.strip().lower()


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
