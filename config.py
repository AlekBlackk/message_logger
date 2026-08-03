"""Loading and validation of the .env configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values


class ConfigError(Exception):
    """Raised when the configuration is missing or malformed."""


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    session_name: str
    watch_chats: tuple[str, ...]
    ignore_chats: tuple[str, ...]
    log_target: str
    log_outgoing: bool
    log_edits: bool
    download_media: bool
    max_media_mb: int
    media_dir: Path
    db_path: Path
    retention_days: int
    backfill_on_start: bool
    backfill_limit: int
    proxy_enabled: bool
    proxy_type: str
    proxy_host: str
    proxy_port: int

    @property
    def max_media_bytes(self) -> int:
        return self.max_media_mb * 1024 * 1024

    def proxy_dict(self) -> dict | None:
        """Telethon proxy argument in python-socks dict format."""
        if not self.proxy_enabled:
            return None
        return {
            "proxy_type": self.proxy_type,
            "addr": self.proxy_host,
            "port": self.proxy_port,
        }


def parse_chat_list(raw: str) -> tuple[str, ...]:
    """Split a comma-separated chat list, dropping blanks and stray spaces."""
    return tuple(item.strip() for item in raw.split(",") if item.strip())


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _as_bool(key: str, raw: str, default: bool) -> bool:
    raw = (raw or "").strip().lower()
    if not raw:
        return default
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ConfigError(f"{key} must be a boolean (true/false), got {raw!r}")


def _as_int(values: dict, key: str, default: int | None) -> int:
    raw = values.get(key, "")
    raw = raw.strip() if raw else ""
    if not raw:
        if default is None:
            raise ConfigError(f"{key} is required — fill it in .env")
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc


def load_config(env_path: str = ".env") -> Config:
    """Read env_path (falling back to the process environment) into a Config."""
    values: dict[str, str] = {}
    values.update({k: v for k, v in os.environ.items()})
    values.update({k: v for k, v in dotenv_values(env_path).items() if v is not None})

    api_hash = (values.get("API_HASH") or "").strip()
    if not api_hash:
        raise ConfigError("API_HASH is required — get it at https://my.telegram.org")

    api_id = _as_int(values, "API_ID", None)
    if api_id <= 0:
        raise ConfigError(f"API_ID must be a positive integer, got {api_id}")

    max_media_mb = _as_int(values, "MAX_MEDIA_MB", 50)
    if max_media_mb < 0:
        raise ConfigError(f"MAX_MEDIA_MB must be >= 0, got {max_media_mb}")

    retention_days = _as_int(values, "RETENTION_DAYS", 14)
    if retention_days < 1:
        raise ConfigError(f"RETENTION_DAYS must be >= 1, got {retention_days}")

    backfill_limit = _as_int(values, "BACKFILL_LIMIT", 200)
    if backfill_limit < 0:
        raise ConfigError(f"BACKFILL_LIMIT must be >= 0, got {backfill_limit}")

    proxy_port = _as_int(values, "PROXY_PORT", 1080)
    if not (1 <= proxy_port <= 65535):
        raise ConfigError(f"PROXY_PORT must be between 1 and 65535, got {proxy_port}")

    return Config(
        api_id=api_id,
        api_hash=api_hash,
        session_name=(values.get("SESSION_NAME") or "logger").strip(),
        watch_chats=parse_chat_list(values.get("WATCH_CHATS") or ""),
        ignore_chats=parse_chat_list(values.get("IGNORE_CHATS") or ""),
        log_target=(values.get("LOG_TARGET") or "me").strip(),
        log_outgoing=_as_bool("LOG_OUTGOING", values.get("LOG_OUTGOING") or "", True),
        log_edits=_as_bool("LOG_EDITS", values.get("LOG_EDITS") or "", True),
        download_media=_as_bool(
            "DOWNLOAD_MEDIA", values.get("DOWNLOAD_MEDIA") or "", True
        ),
        max_media_mb=max_media_mb,
        media_dir=Path((values.get("MEDIA_DIR") or "media").strip()),
        db_path=Path((values.get("DB_PATH") or "messages.db").strip()),
        retention_days=retention_days,
        backfill_on_start=_as_bool(
            "BACKFILL_ON_START", values.get("BACKFILL_ON_START") or "", True
        ),
        backfill_limit=backfill_limit,
        proxy_enabled=_as_bool("PROXY_ENABLED", values.get("PROXY_ENABLED") or "", False),
        proxy_type=(values.get("PROXY_TYPE") or "socks5").strip(),
        proxy_host=(values.get("PROXY_HOST") or "127.0.0.1").strip(),
        proxy_port=proxy_port,
    )
