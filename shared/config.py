from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

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
    # Плейбук Авито (см. shared/docs_context.py) — вендорится ВНУТРИ этого
    # репозитория (avito_playbook/docs/), а не лежит на диске отдельно, как
    # два репо выше — поэтому дефолт вычисляется относительно расположения
    # этого файла, а не захардкожен под конкретную машину разработки. Пусто
    # (явно через env) → ассистент не подключает плейбук вообще.
    avito_playbook_path: str = field(
        default_factory=lambda: _optional(
            "AVITO_PLAYBOOK_PATH",
            str(Path(__file__).resolve().parent.parent / "avito_playbook" / "docs"),
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
    # Час (в субботу, по локальному времени машины) итогов недельного спринта
    # в TEAM_CHAT_ID — см. team_bot/main.py sprint_loop. Не шлётся, если
    # team_chat_id не задан. Строится БЕЗ единого вызова LLM (см. R-COST в
    # sprint_loop) — только структурные данные доски, поэтому по просьбе не
    # тратит бюджет OpenAI вообще.
    sprint_hour: int = field(default_factory=lambda: int(_optional("TEAM_SPRINT_HOUR", "19")))
    # Транскрибация записей встреч (см. shared/transcription_client.py,
    # team_bot/main.py handle_meeting_recording) — сервис Nexara (api.nexara.ru),
    # не OpenAI: поддерживает диаризацию (task=diarize, кто что сказал) и файлы
    # до 3 ГБ вместо лимита OpenAI в 25 МБ. Пусто → хендлер честно говорит, что
    # транскрибация не настроена, вместо падения при первой пересланной записи.
    nexara_api_key: str = field(default_factory=lambda: _optional("NEXARA_API_KEY"))
    # Час (по локальному времени машины) ежевечернего дайджеста продуктовых
    # метрик (DAU/WAU/MAU, retention, воронка онбординга, paywall) в
    # TEAM_CHAT_ID — см. team_bot/main.py metrics_loop. Не шлётся, если
    # team_chat_id не задан, или если ADMIN_USERNAME/ADMIN_PASSWORD не заданы
    # (честно логирует причину вместо падения).
    metrics_hour: int = field(default_factory=lambda: int(_optional("TEAM_METRICS_HOUR", "21")))
    # Basic-auth в защищённую админку бэкенда (/admin/dashboard.json,
    # /admin/persons.json) — см. shared/metrics_digest.py. Пусто → дайджест
    # метрик отключён (не пытается запрашивать без кредов).
    admin_base_url: str = field(default_factory=lambda: _optional("ADMIN_BASE_URL", "https://api.kubyshka.app"))
    admin_username: str = field(default_factory=lambda: _optional("ADMIN_USERNAME"))
    admin_password: str = field(default_factory=lambda: _optional("ADMIN_PASSWORD"))
    # Час (в субботу утром, по локальному времени машины) еженедельного
    # дайджеста инсайтов из фин-каналов в TEAM_CHAT_ID — см.
    # team_bot/news_digest.py + team_bot/main.py news_digest_loop. Утро (не
    # вечер, как sprint/metrics) — просьба founder'а: "собирал инфу за неделю
    # ... в субботу утром".
    news_digest_hour: int = field(default_factory=lambda: int(_optional("TEAM_NEWS_DIGEST_HOUR", "9")))
    # Публичные Telegram-каналы (без @, через запятую) — источники для
    # еженедельного дайджеста. Намеренно РАЗНОГО формата (не 10 клонов одного
    # жанра): @finance_pro_tg/@nastya_docs — авторские блоги про личные
    # финансы; @finmeme — мемный/ироничный (в духе Aviasales) взгляд на рынок;
    # @bankiruofficial — новости банковских продуктов/ставок; @russianmacro,
    # @cbonds — макро/долговой рынок для контекста. Каждый явно проверен на
    # отсутствие политики/войны в постах (2026) — при добавлении новых
    # каналов эту проверку повторять обязательно, жанр дайджеста строго
    # неполитический. Список можно менять в .env без правки кода.
    news_digest_channels: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            c.strip().lstrip("@")
            for c in _optional(
                "TEAM_NEWS_DIGEST_CHANNELS",
                "finance_pro_tg,multievan,banksta,markettwits,nebrexnya,"
                "finmeme,bankiruofficial,nastya_docs,russianmacro,cbonds",
            ).split(",")
            if c.strip()
        )
    )
    # Username'ы (без @), которым разрешено запускать /custdev — см.
    # team_bot/custdev.py. Founder — главный админ по умолчанию (fullbyte9),
    # плюс остальные явно перечисленные в .env через запятую.
    custdev_admin_usernames: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            u.strip().lstrip("@")
            for u in _optional("TEAM_CUSTDEV_ADMINS", "fullbyte9,varushkaushko,popov_kirill_a").split(",")
            if u.strip()
        )
    )

    @property
    def docs_paths(self) -> list[str]:
        paths = [self.finassist_docs_path]
        if self.finik_backend_docs_path:
            paths.append(self.finik_backend_docs_path)
        if self.avito_playbook_path:
            paths.append(self.avito_playbook_path)
        return paths


@dataclass(frozen=True)
class ChannelBotConfig:
    telegram_token: str = field(default_factory=lambda: _require("CHANNEL_BOT_TOKEN"))
    channel_id: str = field(default_factory=lambda: _require("CHANNEL_ID"))
    # Второй, отдельный канал "про нас как команду" — тот же бот, тот же
    # процесс/контент-пайплайн, другой chat_id (см. main.py: /postnow team,
    # /preview team, /draft team). Пусто → команды с "team" честно отвечают,
    # что канал не настроен, вместо попытки постить в channel_id по ошибке.
    # Не входит в _WEEKLY_SLOTS — сознательно НЕ автопостится по расписанию,
    # пока не появится ручная обкатка формата (см. main.py — тот же принцип
    # осторожности, что и при первом запуске основного channel_bot).
    team_channel_id: str = field(default_factory=lambda: _optional("TEAM_CHANNEL_ID"))
    discussion_chat_id: str = field(default_factory=lambda: _optional("DISCUSSION_CHAT_ID"))
    finassist_docs_path: str = field(default_factory=lambda: _require("FINASSIST_DOCS_PATH"))
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
    # По умолчанию канал постит полностью автономно (осознанный выбор,
    # см. README). Опционально можно включить черновик-на-ревью: перед
    # публикацией пост уходит в admin_chat_id с кнопками "Опубликовать"/
    # "Пропустить" вместо немедленной публикации. Без admin_chat_id эта
    # опция бессмысленна (некуда слать черновик) — тихо игнорируется.
    require_approval: bool = field(default_factory=lambda: _optional("CHANNEL_REQUIRE_APPROVAL", "0") == "1")
    # Ссылка на бета-тест приложения — иногда (не на каждый пост) добавляется
    # в конец поста как приглашение (см. content_generator.py). Пусто →
    # приглашения не будет (не выдумываем несуществующую ссылку).
    beta_invite_url: str = field(default_factory=lambda: _optional("BETA_INVITE_URL"))


@dataclass(frozen=True)
class TaskBoardConfig:
    # Мини-апп с общей доской задач — sqlite как хранилище (см. shared/task_store.py).
    db_path: str = field(default_factory=lambda: _optional("TASKS_DB_PATH", "kubyshka_tasks.db"))
    # Публичный https-адрес, где хостится webapp/server.py (Telegram требует
    # https для web_app-кнопок — localhost не подходит). Пусто → /board в
    # team_bot сообщает, что мини-апп ещё не задеплоен, вместо падения.
    webapp_url: str = field(default_factory=lambda: _optional("WEBAPP_URL"))
    webapp_port: int = field(default_factory=lambda: int(_optional("WEBAPP_PORT", "8080")))
    # Где хранится момент окончания последнего спринта (см.
    # shared/sprint_state.py) — общий путь для team_bot (двигает границу раз
    # в неделю) и webapp (только читает её для отображения на доске), это
    # два независимо деплоящихся процесса, файл — их общая точка синхронизации.
    sprint_state_path: str = field(default_factory=lambda: _optional("SPRINT_STATE_PATH", "team_bot_last_sprint.json"))
    # Фото, прикреплённые к задачам (team_bot/main.py cmd_photo/cmd_task) —
    # лежат прямо под webapp/static, чтобы отдавать их без отдельного роута:
    # webapp/server.py уже монтирует StaticFiles на /static.
    photos_dir: str = field(default_factory=lambda: _optional("TASK_PHOTOS_DIR", "webapp/static/task_photos"))


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
