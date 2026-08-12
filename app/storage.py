"""Хранение состояния на диске.

Пока это простые JSON-файлы в томе. Нагрузка тут копеечная — десятки записей в
сутки, — поэтому база данных избыточна. Если понадобится, замена изолирована
этим модулем.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, payload: Any) -> None:
    """Атомарная запись: сначала во временный файл, потом подмена.

    Иначе передеплой или падение посреди записи оставит битый файл с токенами,
    и сервис не поднимется без ручного вмешательства.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with open(handle, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
        tmp_path.replace(path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
