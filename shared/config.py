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
    # Опционально: второй репозиторий (бэкенд) для контекста ассистента.
    # Пусто → ассистент видит только FinAssist. По умолчанию — путь на этой
    # машине, где оба репо лежат рядом в рамках одной сессии разработки.
    finik_backend_docs_path: str = field(
        default_factory=lambda: _optional(
            "FINIK_BACKEND_DOCS_PATH", "/Users/arakcheevpm/Desktop/Кубышка/Finik-backend/docs"
        )
    )

    # R-COST: команд-бот — низкие ставки (внутренний Q&A), можно посадить на
    # более дешёвую/быструю модель, не трогая ту, что настроена для канала.
    # Пусто → берётся общий OPENROUTER_MODEL.
    model_override: str = field(default_factory=lambda: _optional("TEAM_BOT_MODEL"))
    # R-COST: не более N вопросов ассистенту в час НА ЧАТ — страховка от
    # случайного/нарочного вычерпывания бюджета в бытовом чате. 0 = выключено.
    max_questions_per_hour: int = field(
        default_factory=lambda: int(_optional("TEAM_BOT_MAX_QUESTIONS_PER_HOUR", "30"))
    )

    @property
    def docs_paths(self) -> list[str]:
        paths = [self.finassist_docs_path]
        if self.finik_backend_docs_path:
            paths.append(self.finik_backend_docs_path)
        return paths


@dataclass(frozen=True)
class ChannelBotConfig:
    telegram_token: str = field(default_factory=lambda: _require("CHANNEL_BOT_TOKEN"))
    channel_id: str = field(default_factory=lambda: _require("CHANNEL_ID"))
    discussion_chat_id: str = field(default_factory=lambda: _optional("DISCUSSION_CHAT_ID"))
    finassist_docs_path: str = field(default_factory=lambda: _require("FINASSIST_DOCS_PATH"))
    post_interval_hours: int = field(default_factory=lambda: int(_optional("POST_INTERVAL_HOURS", "24")))
    # Чат, откуда разрешены админ-команды (/postnow, /queue, /status,
    # /removetopic) — обычно тот же чат, что TEAM_CHAT_ID у team_bot. Не
    # задан → команды доступны из любого чата (ок для локальной разработки,
    # не для продакшена с открытым чатом обсуждения).
    admin_chat_id: str = field(default_factory=lambda: _optional("CHANNEL_ADMIN_CHAT_ID"))


@dataclass(frozen=True)
class TaskBoardConfig:
    # Мини-апп с общей доской задач — sqlite как хранилище (см. shared/task_store.py).
    db_path: str = field(default_factory=lambda: _optional("TASKS_DB_PATH", "kubyshka_tasks.db"))
    # Публичный https-адрес, где хостится webapp/server.py (Telegram требует
    # https для web_app-кнопок — localhost не подходит). Пусто → /board в
    # team_bot сообщает, что мини-апп ещё не задеплоен, вместо падения.
    webapp_url: str = field(default_factory=lambda: _optional("WEBAPP_URL"))
    webapp_port: int = field(default_factory=lambda: int(_optional("WEBAPP_PORT", "8080")))


@dataclass(frozen=True)
class LLMConfig:
    # OpenRouter — OpenAI-совместимый API, но другой base_url и модели с
    # префиксом провайдера (например "openai/gpt-5-mini", "anthropic/claude-...").
    api_key: str = field(default_factory=lambda: _require("OPENROUTER_API_KEY"))
    model: str = field(default_factory=lambda: _optional("OPENROUTER_MODEL", "openai/gpt-5-mini"))
    base_url: str = field(default_factory=lambda: _optional("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"))
