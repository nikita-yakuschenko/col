"""Проверка ключевой предпосылки: живёт ли сессия ДомКлик вне браузера.

Берёт куки, выгруженные из профиля Playwright (`npm run export-cookies`), и дёргает
ими чатовый REST обычным HTTP-клиентом. Если приходит 200 со списком комнат —
значит боевому сервису headless-браузер в постоянной работе не нужен.

Запуск:  .venv\\Scripts\\python.exe scripts\\probe_session.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "storage-state.json"

CHAT_API = "https://ipoteka.domclick.ru/chat/api/v3"
CABINET = "https://homeland-projects.domclick.ru"

# Браузерный набор заголовков: API за Sber ID бывает придирчив к безголовым клиентам.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Origin": CABINET,
    "Referer": f"{CABINET}/",
    # Без него чатовый API отвечает 400: "'X-Service' header is mandatory".
    # Значение подсмотрено у виджета — им же он представляется сам.
    "X-Service": "web/widget/1.0.1/build-number-null",
}


def load_cookies() -> dict[str, str]:
    if not STATE_PATH.exists():
        sys.exit(f"Нет {STATE_PATH}. Сначала: cd recon && npm run export-cookies")
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    cookies = {c["name"]: c["value"] for c in state["cookies"] if "domclick.ru" in c["domain"]}
    if not cookies:
        sys.exit("В storage-state.json нет кук домена domclick.ru")
    return cookies


def main() -> int:
    cookies = load_cookies()
    print(f"Кук для domclick.ru: {len(cookies)}\n")

    with httpx.Client(headers=HEADERS, cookies=cookies, timeout=20.0, follow_redirects=False) as client:
        response = client.get(f"{CHAT_API}/rooms", params={"list": "main", "limit": 50, "offset": 0})
        print(f"GET /rooms -> {response.status_code}")

        if response.status_code != 200:
            # Редирект на страницу входа — самый вероятный симптом протухшей сессии.
            location = response.headers.get("location")
            print(f"  location: {location}" if location else f"  тело: {response.text[:400]}")
            print("\nСессия не принята вне браузера — нужен другой способ переиспользовать вход.")
            return 1

        payload = response.json()
        rooms = payload.get("data") or []
        print(f"Комнат получено: {len(rooms)}\n")

        for room in rooms[:10]:
            tags = {t["name"]: t.get("value") for t in room.get("tags") or []}
            members = room.get("members") or []
            client_member = next(
                (m for m in members if "USER" in (m.get("chatRole") or [])),
                None,
            )
            preview = (room.get("lastMessagePreview") or {}).get("message", "")
            print(f"  room {room.get('_id')}")
            print(f"    клиент:  {client_member.get('displayName') if client_member else '?'}"
                  f"  casId={tags.get('client_cas_id')}")
            print(f"    последнее: {preview[:80]}")

    print("\nСессия работает вне браузера. Headless в постоянной работе не нужен.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
