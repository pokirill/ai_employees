from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def _optional(name: str, default: str = "") -> str:
    return os.getenv(name, default)


@dataclass(frozen=True)
class TeamBotConfig:
    telegram_token: str = field(default_factory=lambda: _require("TEAM_BOT_TOKEN"))
    team_chat_id: str = field(default_factory=lambda: _require("TEAM_CHAT_ID"))
    icloud_apple_id: str = field(default_factory=lambda: _require("ICLOUD_APPLE_ID"))
    icloud_app_password: str = field(default_factory=lambda: _require("ICLOUD_APP_SPECIFIC_PASSWORD"))
    icloud_reminders_list_name: str = field(
        default_factory=lambda: _optional("ICLOUD_REMINDERS_LIST_NAME", "Кубышка — задачи")
    )
    finassist_docs_path: str = field(default_factory=lambda: _require("FINASSIST_DOCS_PATH"))


@dataclass(frozen=True)
class ChannelBotConfig:
    telegram_token: str = field(default_factory=lambda: _require("CHANNEL_BOT_TOKEN"))
    channel_id: str = field(default_factory=lambda: _require("CHANNEL_ID"))
    discussion_chat_id: str = field(default_factory=lambda: _optional("DISCUSSION_CHAT_ID"))
    finassist_docs_path: str = field(default_factory=lambda: _require("FINASSIST_DOCS_PATH"))
    post_interval_hours: int = field(default_factory=lambda: int(_optional("POST_INTERVAL_HOURS", "24")))


@dataclass(frozen=True)
class LLMConfig:
    api_key: str = field(default_factory=lambda: _require("OPENAI_API_KEY"))
    model: str = field(default_factory=lambda: _optional("OPENAI_MODEL", "gpt-5-mini"))
