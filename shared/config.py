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
    # Не используется в коде нигде, кроме документации/референса — не блокируем
    # старт бота её отсутствием.
    team_chat_id: str = field(default_factory=lambda: _optional("TEAM_CHAT_ID"))
    # iCloud — best-effort зеркало (см. team_bot/main.py cmd_task/cmd_done), не
    # обязательное условие для работы доски задач/ассистента. Пусто → /task
    # просто не пытается зеркалировать, доска всё равно работает.
    icloud_apple_id: str = field(default_factory=lambda: _optional("ICLOUD_APPLE_ID"))
    icloud_app_password: str = field(default_factory=lambda: _optional("ICLOUD_APP_SPECIFIC_PASSWORD"))
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
    # Пусто → берётся общий OPENAI_MODEL.
    model_override: str = field(default_factory=lambda: _optional("TEAM_BOT_MODEL"))
    # R-COST: не более N вопросов ассистенту в час НА ЧАТ — страховка от
    # случайного/нарочного вычерпывания бюджета в бытовом чате. 0 = выключено.
    max_questions_per_hour: int = field(
        default_factory=lambda: int(_optional("TEAM_BOT_MAX_QUESTIONS_PER_HOUR", "30"))
    )
    # Час (по локальному времени машины, где крутится бот) ежедневного
    # дайджеста открытых задач в TEAM_CHAT_ID. Не шлётся, если team_chat_id
    # не задан (см. team_bot/main.py reminder_loop).
    reminder_hour: int = field(default_factory=lambda: int(_optional("TEAM_REMINDER_HOUR", "10")))

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
    # R-COST: лимит ответов в чате обсуждения НА ПОЛЬЗОВАТЕЛЯ (не на чат целиком,
    # это публичный community-чат — общий на всех лимит душил бы всех сразу
    # из-за одного активного человека). 0 = выключено.
    discussion_max_replies_per_hour: int = field(
        default_factory=lambda: int(_optional("CHANNEL_DISCUSSION_MAX_REPLIES_PER_HOUR", "10"))
    )


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
    # Прямой OpenAI (не OpenRouter — решили остаться на уже рабочем ключе
    # OpenAI, не заводить отдельный ключ на OpenRouter). Не обязателен для
    # старта бота — команды /task, /tasks, /done, /board не используют LLM
    # вообще; отсутствие ключа падает только при реальной попытке спросить
    # ассистента (см. team_bot/main.py _ask_llm).
    api_key: str = field(default_factory=lambda: _optional("OPENAI_API_KEY"))
    model: str = field(default_factory=lambda: _optional("OPENAI_MODEL", "gpt-5-mini"))
    # Дефолт задан явно, а не пусто: если .env содержит "OPENAI_BASE_URL="
    # (пустая строка), openai SDK читает её напрямую из окружения как есть и
    # НЕ подставляет свой дефолт — пустая строка ломает запросы (httpx:
    # "missing http:// protocol"). Настраиваемо на случай прокси/шлюза.
    base_url: str = field(default_factory=lambda: _optional("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    # R-COST: reasoning-модели (gpt-5*, o1/o3/o4) по умолчанию тратят часть
    # max_tokens на СКРЫТЫЕ reasoning-токены — на реальных вызовах это
    # доводило до пустых ответов (весь бюджет уходил на "раздумья", см.
    # AI_CHANGELOG/память проекта). "minimal" полностью убирает этот налог
    # (проверено реальным вызовом: reasoning_tokens=0, полноценный ответ) —
    # для простых Q&A/постов канала более глубокое рассуждение не нужно.
    reasoning_effort: str = field(default_factory=lambda: _optional("OPENAI_REASONING_EFFORT", "minimal"))
