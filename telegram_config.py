"""Local Telegram settings in instance/telegram.json (outside static/ and Git).

The file holds the bot token in plain text — Telegram needs it for every call, so it
cannot be hashed. Protect it like a password: never commit, share or screenshot it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

import config
from auth import _write_private_file
from telegram_client import looks_like_token


@dataclass(frozen=True)
class TelegramSettings:
    token: str
    chat_id: int
    chat_label: str      # e.g. "Anna (@anna)" — shown in CLI output, never the token
    bot_username: str

    def __repr__(self) -> str:  # never show the token, even in debug output
        return f"TelegramSettings(bot=@{self.bot_username}, chat_id={self.chat_id})"


def load() -> TelegramSettings | None:
    """Return the settings, or None if Telegram is not configured (or the file is invalid)."""
    try:
        data = json.loads(config.TELEGRAM_CONFIG_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return None
    token, chat_id = data.get("bot_token"), data.get("chat_id")
    if not isinstance(token, str) or not looks_like_token(token):
        return None
    if not isinstance(chat_id, int) or isinstance(chat_id, bool):
        return None
    return TelegramSettings(
        token=token,
        chat_id=chat_id,
        chat_label=str(data.get("chat_label", "")),
        bot_username=str(data.get("bot_username", "")),
    )


def is_configured() -> bool:
    return load() is not None


def save(token: str, chat_id: int, chat_label: str, bot_username: str) -> None:
    payload = {
        "bot_token": token,
        "chat_id": chat_id,
        "chat_label": chat_label,
        "bot_username": bot_username,
        "configured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _write_private_file(config.TELEGRAM_CONFIG_FILE, json.dumps(payload, indent=2) + "\n")


def remove() -> bool:
    try:
        config.TELEGRAM_CONFIG_FILE.unlink()
        return True
    except FileNotFoundError:
        return False
