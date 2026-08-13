"""Достаёт системный токен хранилища ДомКлик из перехваченного трафика.

Значение нужно положить в переменную DOMCLICK_STORAGE_TOKEN — без неё сервис
не сможет отправлять вложения оператора клиенту.

Запуск:  .venv\\Scripts\\python.exe scripts\\show_storage_token.py
"""

from __future__ import annotations

import base64
import glob
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    dumps = sorted(glob.glob(str(ROOT / "recon-out" / "*" / "http.ndjson")), reverse=True)
    if not dumps:
        print("Дампов нет. Сначала: cd recon && npm run capture-files")
        return 1

    for dump in dumps:
        for line in Path(dump).read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Токен берём именно из запроса к хранилищу: на других эндпоинтах
            # ходит другой, короткий ключ, и с ним загрузка отвечает 403.
            if "/storage/files" not in record.get("url", ""):
                continue
            token = (record.get("requestHeaders") or {}).get("x-access-token")
            if not token:
                continue

            print(f"# из сессии {Path(dump).parent.name}")
            _describe(token)
            print("\nDOMCLICK_STORAGE_TOKEN=" + token)
            return 0

    print("В дампах нет запросов к /storage/files. Пришлите файл в чате при включённой")
    print("записи: cd recon && npm run capture-files")
    return 1


def _describe(token: str) -> None:
    parts = token.split(".")
    if len(parts) != 3:
        print(f"# токен не похож на JWT, длина {len(token)}")
        return
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, json.JSONDecodeError):
        print("# JWT, но полезная нагрузка не разобралась")
        return
    print(f"# JWT, поля: {sorted(claims)}")
    if "exp" not in claims:
        print("# срока действия нет — токен постоянный")


if __name__ == "__main__":
    sys.exit(main())
