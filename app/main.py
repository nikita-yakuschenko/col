"""Мост «ДомКлик → Открытые линии Битрикс24».

Веб-часть: эндпоинты для Битрикса (установка, события, настройки коннектора)
и служебная страница загрузки кук ДомКлик. Живая часть — в app/runtime.py.
"""

from __future__ import annotations

import html
import json
import logging
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from urllib.parse import quote, urlparse

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from app import state
from app.bitrix import BitrixClient, BitrixError, register_connector
from app.bridge import deliver_operator_reply, parse_bracketed
from app.config import settings
from app.runtime import runtime
from app.storage import read_json, write_json

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
# httpx пишет в лог полный URL каждого запроса вместе с query-параметрами —
# туда попадают токены и ссылки на файлы. Оставляем только предупреждения.
logging.getLogger("httpx").setLevel(logging.WARNING)

log = logging.getLogger("bridge")

# Одноразовые ключи для формы загрузки кук: nonce -> момент истечения.
_nonces: dict[str, float] = {}

# Счётчики событий: без них отклонённое событие видно только в логах контейнера.
_events: dict[str, object] = {"received": 0, "rejected": 0, "last": "", "last_reject_reason": ""}


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    log.info("Старт. Данные: %s", settings.data_dir)
    _migrate_tokens()
    if settings.missing:
        log.warning("Не заданы переменные окружения: %s", ", ".join(settings.missing))
    await runtime.start()
    yield
    await runtime.stop()
    log.info("Остановка")


app = FastAPI(title="ДомКлик → Открытые линии", lifespan=lifespan, docs_url=None, redoc_url=None)


@app.get("/health")
async def health() -> JSONResponse:
    tokens = read_json(settings.tokens_path, default={})
    return JSONResponse(
        {
            "status": "ok",
            "time": datetime.now(timezone.utc).isoformat(),
            "b24_configured": settings.is_b24_configured,
            "b24_installed": bool(tokens.get("access_token")),
            # Диагностика окружения: без неё пустая переменная выясняется только
            # по отказу формы, и непонятно, чей это косяк — кода или деплоя.
            "env_missing": settings.missing,
            "admin_upload": bool(settings.admin_token),
            "connector_id": settings.connector_id,
            "connector_registered": read_json(_register_report_path(), default={}),
            "line": state.read_line(),
            "events": dict(_events),
            "domclick": runtime.snapshot(),
        }
    )


# --- Битрикс: установка -----------------------------------------------------

@app.api_route("/b24/install", methods=["GET", "POST"])
async def b24_install(request: Request) -> HTMLResponse:
    """Битрикс присылает токены формой и ждёт страницу с BX24.installFinish()."""
    payload = await _payload(request)
    saved = _save_auth(payload, source="install")
    log.info("Установка через страницу: токены %s", "сохранены" if saved else "не пришли")
    if saved:
        await _register_connector_safely()
    return HTMLResponse(_INSTALL_PAGE)


@app.api_route("/b24/handler", methods=["GET", "POST"])
async def b24_handler(request: Request):
    """Единая точка: встройка настроек и события открытых линий."""
    payload = await _payload(request)

    if payload.get("PLACEMENT"):
        _save_auth(payload, source="placement")
        return HTMLResponse(await _settings_page(payload))

    event = (payload.get("event") or "").upper()
    if not event:
        log.info("Запрос без события и встройки, ключи=%s", sorted(payload))
        return JSONResponse({"ok": True})

    if event == "ONAPPINSTALL":
        saved = _save_auth(payload, source="event")
        log.info("Установка событием: токены %s", "сохранены" if saved else "отклонены")
        if saved:
            await _register_connector_safely()
        return JSONResponse({"ok": saved})

    _events["received"] = int(_events["received"]) + 1
    _events["last"] = event

    verified, reason = _verify_event(payload)
    if not verified:
        _events["rejected"] = int(_events["rejected"]) + 1
        _events["last_reject_reason"] = reason
        log.warning("Событие %s отклонено: %s", event, reason)
        return JSONResponse({"ok": False, "error": reason}, status_code=403)

    if event == "ONAPPUNINSTALL":
        write_json(settings.tokens_path, {})
        log.warning("Приложение удалено, токены очищены")
        return JSONResponse({"ok": True})

    data = parse_bracketed(payload).get("data") or {}

    if event == "ONIMCONNECTORMESSAGEADD":
        if runtime.dc is None:
            log.error("Ответ оператора некуда отправлять: нет сессии ДомКлик")
            return JSONResponse({"ok": False, "error": "no domclick session"})
        delivered = await deliver_operator_reply(runtime.dc, BitrixClient(), data)
        return JSONResponse({"ok": True, "delivered": delivered})

    if event in ("ONIMCONNECTORSTATUSDELETE", "ONIMCONNECTORLINEDELETE"):
        # Родная кнопка «Отключить» в Битриксе не спрашивает нас — узнаём событием,
        # иначе наше состояние разъедется с настройками линии.
        current = state.read_line()
        if current.get("line"):
            state.write_line(int(current["line"]), active=False)
        log.warning("Коннектор отключён на стороне Битрикса (%s)", event)
        return JSONResponse({"ok": True})

    log.info("Событие %s принято без обработки", event)
    return JSONResponse({"ok": True})


# --- служебное: куки ДомКлик ------------------------------------------------

@app.get("/f/{token}")
async def serve_attachment(token: str):
    """Отдаёт вложение из ДомКлик по одноразовой ссылке.

    Оригинальные ссылки требуют кук сессии, а Битрикс качает файлы анонимно.
    Поэтому он приходит сюда, а мы уже забираем файл с авторизацией.
    """
    link = state.lookup_file(token)
    if not link:
        return JSONResponse({"error": "not found or expired"}, status_code=404)
    if runtime.dc is None:
        return JSONResponse({"error": "no domclick session"}, status_code=503)

    try:
        content, content_type = await runtime.dc.fetch_file(link["url"])
    except Exception as error:  # noqa: BLE001
        log.exception("Не удалось скачать вложение")
        return JSONResponse({"error": str(error)}, status_code=502)

    name = str(link.get("name") or "file")
    ascii_name = name.encode("ascii", "ignore").decode() or "file"
    return Response(
        content=content,
        media_type=content_type,
        headers={
            "Content-Disposition": (
                f'inline; filename="{ascii_name}"; '
                f"filename*=UTF-8''{quote(name)}"
            )
        },
    )


@app.get("/admin/cookies")
async def admin_cookies_form() -> HTMLResponse:
    """Страница загрузки сессии ДомКлик.

    Вход в кабинет требует SMS, автоматически сервер сессию получить не может.
    Поэтому раз в N дней сюда приносят свежий storage-state.json.
    """
    return HTMLResponse(_cookies_page(_issue_nonce()))


@app.post("/admin/cookies")
async def admin_cookies_upload(request: Request) -> HTMLResponse:
    form = await request.form()
    if not settings.admin_token:
        return HTMLResponse(_notice_page("ADMIN_TOKEN не задан в окружении — загрузка отключена."))
    if not _consume_nonce(str(form.get("nonce", ""))):
        return HTMLResponse(_notice_page("Форма устарела, обновите страницу."))
    # compare_digest падает на строках с не-ASCII, поэтому сравниваем байты:
    # иначе пароль с кириллицей ронял бы страницу пятисоткой.
    supplied = str(form.get("token", "")).encode("utf-8")
    if not secrets.compare_digest(supplied, settings.admin_token.encode("utf-8")):
        log.warning("Загрузка кук отклонена: неверный токен")
        return HTMLResponse(_notice_page("Неверный токен."))

    upload = form.get("state")
    raw = (await upload.read()).decode("utf-8") if hasattr(upload, "read") else str(form.get("raw", ""))
    try:
        parsed = json.loads(raw)
        cookies = [c for c in parsed.get("cookies", []) if "domclick.ru" in c.get("domain", "")]
    except (json.JSONDecodeError, AttributeError):
        return HTMLResponse(_notice_page("Это не похоже на storage-state.json."))
    if not cookies:
        return HTMLResponse(_notice_page("В файле нет кук домена domclick.ru."))

    write_json(state.cookies_path(), {"cookies": cookies})
    log.info("Загружено кук ДомКлик: %s", len(cookies))
    await runtime.restart()
    return HTMLResponse(
        _notice_page(f"Принято кук: {len(cookies)}. Состояние моста: {runtime.status}.")
    )


# --- коннектор --------------------------------------------------------------

def _register_report_path():
    return settings.data_dir / "connector_registration.json"


async def _register_connector_safely() -> None:
    """Ошибку наружу не выпускаем: установка важнее, а причина видна в /health."""
    report: dict[str, object] = {"at": datetime.now(timezone.utc).isoformat()}
    try:
        report["result"] = await register_connector(BitrixClient())
        report["ok"] = True
        log.info("Коннектор зарегистрирован: %s", report["result"])
    except BitrixError as error:
        report.update({"ok": False, "error": error.code, "description": error.description})
        log.error("Регистрация коннектора не удалась: %s", error)
    except Exception as error:  # noqa: BLE001
        report.update({"ok": False, "error": type(error).__name__, "description": str(error)})
        log.exception("Регистрация коннектора упала")
    write_json(_register_report_path(), report)


# --- вспомогательное --------------------------------------------------------

def _migrate_tokens() -> None:
    """Разовый перенос старого формата токенов.

    Раньше в application_token попадал APP_SID со страницы установки — другое
    значение, из-за которого проверка отбивала все события. Сбрасываем его,
    чтобы настоящий токен подхватился из первого же события.
    """
    tokens = read_json(settings.tokens_path, default={}) or {}
    if not tokens or "app_sid" in tokens:
        return
    tokens["app_sid"] = tokens.get("application_token", "")
    tokens["application_token"] = ""
    write_json(settings.tokens_path, tokens)
    log.warning("Токены переведены в новый формат, application_token сброшен")


def _issue_nonce() -> str:
    now = time.time()
    for key, expires in list(_nonces.items()):
        if expires < now:
            _nonces.pop(key, None)
    nonce = secrets.token_urlsafe(24)
    _nonces[nonce] = now + 900
    return nonce


def _consume_nonce(nonce: str) -> bool:
    expires = _nonces.pop(nonce, None)
    return bool(expires and expires > time.time())


async def _payload(request: Request) -> dict[str, str]:
    if request.method == "POST":
        return {key: str(value) for key, value in (await request.form()).items()}
    return dict(request.query_params)


def _save_auth(payload: dict[str, str], *, source: str) -> bool:
    """Битрикс шлёт авторизацию двумя способами: плоскими полями при установке
    через браузер и вложенным auth[...] в событиях. Поддерживаем оба."""
    access_token = payload.get("AUTH_ID") or payload.get("auth[access_token]")
    if not access_token:
        return False

    domain = payload.get("DOMAIN") or payload.get("auth[domain]", "")
    if domain and not _is_our_portal(domain):
        log.warning("Запрос с чужого портала отклонён: %s", domain)
        return False

    previous = read_json(settings.tokens_path, default={}) or {}
    # APP_SID со страницы установки — это НЕ application_token из событий.
    # Путать их нельзя: иначе проверка событий будет отбивать всё подряд.
    application_token = payload.get("auth[application_token]") or previous.get("application_token", "")

    write_json(
        settings.tokens_path,
        {
            "access_token": access_token,
            "refresh_token": payload.get("REFRESH_ID") or payload.get("auth[refresh_token]", ""),
            "expires_in": payload.get("AUTH_EXPIRES") or payload.get("auth[expires_in]", ""),
            "member_id": payload.get("member_id") or payload.get("auth[member_id]", ""),
            "domain": domain,
            "app_sid": payload.get("APP_SID", ""),
            "application_token": application_token,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "source": source,
        },
    )
    log.info("Токены сохранены (%s), портал %s", source, domain or "?")
    return True


def _verify_event(payload: dict[str, str]) -> tuple[bool, str]:
    """Проверяет, что событие действительно от нашего портала.

    Путь обработчика публичный, поэтому пускать всё подряд нельзя. Но и жёстко
    требовать совпадения с APP_SID нельзя: при установке через браузерную
    страницу Битрикс отдаёт APP_SID, а в событиях присылает application_token,
    и это разные значения. Поэтому опираемся на портал и member_id, а токен
    запоминаем с первого события и дальше уже сверяем.
    """
    tokens = read_json(settings.tokens_path, default={})
    domain = payload.get("auth[domain]") or payload.get("DOMAIN", "")
    member = payload.get("auth[member_id]") or payload.get("member_id", "")
    incoming = payload.get("auth[application_token]") or payload.get("application_token", "")

    if domain and not _is_our_portal(domain):
        return False, f"чужой портал: {domain}"
    if member and tokens.get("member_id") and member != tokens["member_id"]:
        return False, "не совпал member_id"
    if not domain and not member:
        return False, "нет ни портала, ни member_id"

    stored = tokens.get("application_token") or ""
    if stored and incoming:
        if not secrets.compare_digest(stored.encode(), incoming.encode()):
            return False, "не совпал application_token"
        return True, ""

    if incoming and not stored:
        # Первое событие: запоминаем токен, дальше сверяем уже по нему.
        tokens["application_token"] = incoming
        write_json(settings.tokens_path, tokens)
        log.info("application_token получен из события и сохранён")
    return True, ""


def _is_our_portal(domain: str) -> bool:
    expected = urlparse(settings.b24_portal).netloc or settings.b24_portal
    return domain.strip().lower() == expected.strip().lower()


# --- страницы ---------------------------------------------------------------

_PAGE_HEAD = """<!doctype html>
<html lang="ru">
<head><meta charset="utf-8"><title>ДомКлик</title>
<style>
  body { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; padding: 24px; color: #333; }
  h3 { margin: 0 0 4px; }
  .muted { color: #7a8b95; font-size: 13px; margin: 0 0 20px; }
  .row { margin: 12px 0; }
  label { display: block; font-size: 13px; color: #55606a; margin-bottom: 4px; }
  input[type=text], input[type=password], input[type=file] { font: inherit; padding: 7px 9px;
    border: 1px solid #c6cdd2; border-radius: 4px; width: 320px; max-width: 100%; }
  button { font: inherit; padding: 9px 18px; border: 0; border-radius: 4px; cursor: pointer;
    background: #1ab248; color: #fff; }
  .status { padding: 10px 14px; border-radius: 4px; background: #eef2f4; display: inline-block; }
  .status.active { background: #e6f6ec; color: #14803a; }
</style>
</head>
<body>
"""

_PAGE_TAIL = """
  <script src="//api.bitrix24.com/api/v1/"></script>
  <script>if (window.BX24) { BX24.init(function () { BX24.fitWindow(); }); }</script>
</body>
</html>
"""

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


async def _ensure_activated(line: int) -> dict[str, object]:
    """Активирует коннектор на линии через REST.

    Родная кнопка в карточке коннектора переключает только вид: вызвать
    imconnector.activate обязано само приложение, иначе отправка сообщений
    падает с NOT_ACTIVE_LINE, хотя в интерфейсе всё выглядит подключённым.
    """
    client = BitrixClient()
    try:
        await client.call(
            "imconnector.activate",
            {"CONNECTOR": settings.connector_id, "LINE": line, "ACTIVE": "1"},
        )
        log.info("Коннектор активирован на линии %s", line)
        return {"ok": True, "at": datetime.now(timezone.utc).isoformat()}
    except BitrixError as error:
        log.error("Активация линии %s не прошла: %s", line, error)
        report: dict[str, object] = {
            "ok": False,
            "error": error.code,
            "description": error.description,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        # Если линии с таким id нет, полезно сразу увидеть, какие вообще есть.
        try:
            configs = await client.call("imopenlines.config.list.get", {}) or []
            report["known_lines"] = [
                {"id": item.get("ID"), "name": item.get("LINE_NAME"), "active": item.get("ACTIVE")}
                for item in configs
                if isinstance(item, dict)
            ]
        except Exception:  # noqa: BLE001 — это диагностика, падать из-за неё нельзя
            log.exception("Не удалось получить список открытых линий")
        return report


async def _settings_page(payload: dict[str, str]) -> str:
    """Страница коннектора в слайдере Битрикса.

    Своей кнопки подключения здесь нет намеренно: рядом уже есть родная
    битриксовая, и две кнопки для одного действия только путают. Активацию
    делаем сами, молча, при открытии настроек.
    """
    options: dict[str, object] = {}
    try:
        options = json.loads(payload.get("PLACEMENT_OPTIONS") or "{}")
    except json.JSONDecodeError:
        log.warning("PLACEMENT_OPTIONS не разобрался: %r", payload.get("PLACEMENT_OPTIONS"))

    line = str(options.get("LINE") or "").strip()
    active = str(options.get("ACTIVE_STATUS") or "") in {"1", "Y", "true", "True"}

    if not line.isdigit():
        return _notice_page(
            "Битрикс не передал идентификатор линии. Откройте коннектор из настроек"
            " конкретной открытой линии."
        )

    activation = await _ensure_activated(int(line)) if active else None
    state.write_line(int(line), active, activation)
    log.info("Линия %s, подключение: %s, активация: %s", line, active, activation)

    if not active:
        status = '<span class="status">Не подключён. Нажмите «Подключить» выше.</span>'
    elif activation and activation.get("ok"):
        status = f'<span class="status active">Подключён к линии {html.escape(line)}</span>'
    else:
        detail = html.escape(str((activation or {}).get("error", "неизвестная ошибка")))
        status = (
            f'<span class="status">Линия {html.escape(line)}: Битрикс отклонил активацию'
            f" ({detail}). Подробности в /health.</span>"
        )
    snapshot = runtime.snapshot()
    session = (
        '<span class="status active">Сессия ДомКлик активна</span>'
        if snapshot["connected"]
        else f'<span class="status">ДомКлик: {html.escape(str(snapshot["status"]))}</span>'
    )

    return (
        _PAGE_HEAD
        + f"""
  <h3>ДомКлик</h3>
  <p class="muted">Сообщения из чата подрядчика ДомКлик попадают в эту открытую линию,
     ответы оператора уходят обратно клиенту.</p>
  <div class="row">{status}</div>
  <div class="row">{session}</div>
  <p class="muted">Передано сообщений: {snapshot["forwarded"]}</p>
"""
        + _PAGE_TAIL
    )


def _cookies_page(nonce: str) -> str:
    return _PAGE_HEAD + f"""
  <h3>Сессия ДомКлик</h3>
  <p class="muted">Загрузите storage-state.json, снятый командой
     <code>npm run export-cookies</code>. Вход в кабинет требует SMS,
     поэтому сессию приходится обновлять вручную.</p>
  <form method="post" action="/admin/cookies" enctype="multipart/form-data">
    <input type="hidden" name="nonce" value="{html.escape(nonce)}">
    <div class="row">
      <label>Токен администратора</label>
      <input type="password" name="token" autocomplete="off" required>
    </div>
    <div class="row">
      <label>Файл сессии</label>
      <input type="file" name="state" accept="application/json" required>
    </div>
    <div class="row"><button type="submit">Загрузить</button></div>
  </form>
""" + _PAGE_TAIL


def _notice_page(message: str) -> str:
    return _PAGE_HEAD + f"""
  <h3>ДомКлик</h3>
  <div class="row"><span class="status">{html.escape(message)}</span></div>
""" + _PAGE_TAIL
