"""Настройки сервиса. Всё через переменные окружения — в Dokploy их задают в интерфейсе."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass(frozen=True)
class Settings:
    # Каталог для состояния, которое обязано пережить передеплой:
    # OAuth-токены Битрикса и куки ДомКлик. В контейнере это том.
    data_dir: Path

    # Публичный адрес сервиса — Битрикс ходит сюда за установкой,
    # настройками коннектора и шлёт события.
    public_url: str

    # Локальное приложение Битрикс24.
    b24_portal: str
    b24_client_id: str
    b24_client_secret: str

    # Код коннектора в открытых линиях. Менять после регистрации нельзя.
    connector_id: str

    # Пароль к странице загрузки кук ДомКлик. Пустой — загрузка отключена.
    admin_token: str

    # Системный JWT хранилища файлов ДомКлик. Без него не отправить вложение
    # оператора клиенту. Достаётся из трафика: scripts/show_storage_token.py
    domclick_storage_token: str

    @property
    def tokens_path(self) -> Path:
        return self.data_dir / "b24_tokens.json"

    @property
    def is_b24_configured(self) -> bool:
        return bool(self.b24_client_id and self.b24_client_secret)

    @property
    def missing(self) -> list[str]:
        """Незаполненные переменные. Значений по умолчанию тут нет намеренно:
        адреса и коды принадлежат конкретной установке и в репозитории им не место."""
        required = {
            "PUBLIC_URL": self.public_url,
            "B24_PORTAL": self.b24_portal,
            "CONNECTOR_ID": self.connector_id,
            "B24_CLIENT_ID": self.b24_client_id,
            "B24_CLIENT_SECRET": self.b24_client_secret,
        }
        return [name for name, value in required.items() if not value]


def load_settings() -> Settings:
    return Settings(
        data_dir=Path(_env("DATA_DIR", "/data")),
        public_url=_env("PUBLIC_URL").rstrip("/"),
        b24_portal=_env("B24_PORTAL").rstrip("/"),
        b24_client_id=_env("B24_CLIENT_ID"),
        b24_client_secret=_env("B24_CLIENT_SECRET"),
        connector_id=_env("CONNECTOR_ID"),
        admin_token=_env("ADMIN_TOKEN"),
        domclick_storage_token=_env("DOMCLICK_STORAGE_TOKEN"),
    )


settings = load_settings()
