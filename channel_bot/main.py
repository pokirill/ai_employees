from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from channel_bot.content_generator import (
    GeneratedPost,
    generate_clarifying_questions,
    generate_compose_post,
    generate_feedback_metrics_post,
    generate_intro_post,
    generate_next_post,
    generate_post_for_category,
    revise_post,
)
from channel_bot.content_queue import append_topic, load_queue, save_queue
from channel_bot.feedback_store import add_feedback, load_feedback, remove_feedback
from channel_bot.post_history import record_published_post
from channel_bot.post_state import load_last_post_info, save_last_post_at
from channel_bot.subscriber_snapshot import load_subscriber_snapshot, save_subscriber_snapshot
from shared.chat_log import DEFAULT_LOG_PATH, all_messages, format_for_prompt
from shared.config import ChannelBotConfig, LLMConfig
from shared.docs_context import load_project_context, sync_docs_repo
from shared.llm_client import LLMClient
from shared.rate_limiter import SlidingWindowLimiter

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("channel_bot")

config = ChannelBotConfig()
llm = LLMClient(LLMConfig())

bot = Bot(token=config.telegram_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

_QUEUE_PATH = "channel_bot/topics_queue.json"
_STORY_QUEUE_PATH = "channel_bot/story_topics_queue.json"
_CHANGELOG_STATE_PATH = "channel_bot/used_changelog_titles.json"
_POST_STATE_PATH = "channel_bot/last_post_state.json"
_FEEDBACK_PATH = "channel_bot/feedback_log.json"
_HISTORY_PATH = "channel_bot/post_history_log.json"

# Пересмотрено по просьбе — 2-3 поста В ДЕНЬ оказались избыточны на старте
# (некому/незачем читать и сочинять так часто без аудитории, см. Кирилл: "и
# читать замучаешься, и сочинять" + "аудитории ещё нет, увеличить всегда
# успеем"): вместо ежедневного грида — фиксированные дни недели, по аналогии
# с тренировками (пн/ср/сб), с ЗАКРЕПЛЁННОЙ темой за каждым днём, а не
# случайной. weekday: 0=понедельник ... 5=суббота (см. date.weekday()).
# Одно время на все три дня (полдень МСК) — было не так важно как сами дни,
# при желании поменять — просто другое число здесь.
_WEEKLY_SLOTS: list[tuple[int, int, int, str]] = [
    (0, 12, 0, "feedback_metrics"),  # понедельник — фидбек + отчёт о метриках
    (2, 12, 0, "feature"),  # среда — полезный для аудитории пост
    (5, 12, 0, "release_notes"),  # суббота — релиз-ноуты за неделю
]
_CATEGORY_LABELS = {
    "poll": "опрос",
    "feature": "про фичу",
    "personal": "личная история",
    "feedback_metrics": "фидбек и метрики",
    "release_notes": "релиз-ноуты",
    "intro": "закреплённый интро-пост",
    "icon_story": "история про иконку",
    "team_roster": "кто мы (команда)",
}
# Строка-подсказка форматов для сообщений команд — одно место, чтобы не
# рассинхронизировать несколько скопипащенных литералов при добавлении новой
# категории (уже случалось бы при добавлении feedback_metrics/release_notes).
_CATEGORY_ARG_HINT = "poll|feature|personal|feedback_metrics|release_notes|intro"


def _parse_target(raw_args: str) -> tuple[str, str]:
    """Первое слово аргументов команды может быть "team" — тогда цель "
    команда-канал (config.team_channel_id), а не основной. Возвращает
    ("team"|"main", остаток_строки) — остаток разбирается вызывающим кодом
    как обычно (категория). Команда-канал НЕ входит в _WEEKLY_SLOTS
    (сознательно не автопостится) — доступен только через /postnow team,
    /preview team, /draft team."""
    parts = raw_args.strip().split(maxsplit=1)
    if parts and parts[0].lower() == "team":
        return "team", (parts[1] if len(parts) > 1 else "")
    return "main", raw_args.strip()


def _target_channel_id(target: str) -> str:
    return config.team_channel_id if target == "team" else config.channel_id
# Россия не переходит на летнее время — фиксированный оффсет ок, без tzdata.
_MSK_OFFSET_HOURS = 3

# По просьбе — черновик на апрув должен приходить ЗАРАНЕЕ, а не ровно в
# момент поста, чтобы оставалось время посмотреть/поправить до целевого
# времени. Действует только в режиме ревью — в автономном режиме смотреть
# черновик некому, публикуем ровно в слот.
_APPROVAL_LEAD_MINUTES = 30

_SLOT_OVERRIDES_PATH = "channel_bot/slot_overrides.json"
_LAST_TRIGGERED_SLOT_PATH = "channel_bot/last_triggered_slot.json"


def _next_slot_datetime(now_utc: datetime, *, skip_slot: datetime | None = None) -> tuple[datetime, str]:
    """Ближайший следующий слот (строго после now_utc) из _WEEKLY_SLOTS —
    перебирает ближайшие 8 суток (с запасом на полную неделю + 1), а не
    только "сегодня/завтра" как в старом ежедневном гриде, т.к. следующий
    слот теперь может быть через несколько дней (например, из пятницы до
    понедельника). skip_slot — слот, для которого апрув/публикация УЖЕ
    запрошены в этом цикле (см. _LAST_TRIGGERED_SLOT_PATH) — пропускаем его,
    даже если сам момент слота ещё не наступил (актуально из-за
    _APPROVAL_LEAD_MINUTES: апрув мог быть решён админом раньше целевого
    времени слота)."""
    for day_offset in range(8):
        day = now_utc + timedelta(days=day_offset)
        for weekday, hour_msk, minute_msk, category in _WEEKLY_SLOTS:
            if day.weekday() != weekday:
                continue
            slot_dt = day.replace(hour=hour_msk - _MSK_OFFSET_HOURS, minute=minute_msk, second=0, microsecond=0)
            if day_offset == 0 and slot_dt <= now_utc:
                continue
            if skip_slot is not None and slot_dt == skip_slot:
                continue
            return slot_dt, category
    raise AssertionError("unreachable — _WEEKLY_SLOTS должен быть непустым и покрывать хотя бы один день в неделе")


def _load_last_triggered_slot() -> datetime | None:
    path = Path(_LAST_TRIGGERED_SLOT_PATH)
    if not path.exists():
        return None
    try:
        return datetime.fromisoformat(json.loads(path.read_text(encoding="utf-8"))["slot_at"])
    except Exception:
        return None


def _save_last_triggered_slot(slot_dt: datetime) -> None:
    Path(_LAST_TRIGGERED_SLOT_PATH).write_text(json.dumps({"slot_at": slot_dt.isoformat()}), encoding="utf-8")


def _load_slot_overrides() -> list[dict]:
    path = Path(_SLOT_OVERRIDES_PATH)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return list(data) if isinstance(data, list) else []
    except Exception:
        return []


def _add_slot_override(at: datetime, category: str, *, text: str | None = None) -> None:
    """Разовое переопределение расписания на конкретный момент времени — см.
    /overridepost. `at` — момент, когда СРАЗУ запускать апрув/публикацию (не
    момент самого поста): если хочешь заранее с запасом на ревью — вычти
    _APPROVAL_LEAD_MINUTES сам при вызове, оверрайд эту логику не применяет
    повторно (в отличие от обычного грид-расписания). `text` — если задан,
    в апрув идёт этот ГОТОВЫЙ текст (без генерации LLM), category в этом
    случае используется только для подписи/пометки в сообщении апрува."""
    overrides = _load_slot_overrides()
    entry = {"at": at.isoformat(), "category": category}
    if text:
        entry["text"] = text
    overrides.append(entry)
    overrides.sort(key=lambda o: o["at"])
    Path(_SLOT_OVERRIDES_PATH).write_text(json.dumps(overrides, ensure_ascii=False, indent=2), encoding="utf-8")


def _pop_due_override(now_utc: datetime) -> dict | None:
    overrides = _load_slot_overrides()
    if not overrides:
        return None
    earliest = overrides[0]
    if datetime.fromisoformat(earliest["at"]) > now_utc:
        return None
    Path(_SLOT_OVERRIDES_PATH).write_text(json.dumps(overrides[1:], ensure_ascii=False, indent=2), encoding="utf-8")
    return earliest


def _next_override_at() -> datetime | None:
    overrides = _load_slot_overrides()
    return datetime.fromisoformat(overrides[0]["at"]) if overrides else None

# R-COST: см. shared/rate_limiter.py — защита от одного спамящего "?" в
# публичном чате обсуждения. По пользователю, не по чату целиком (иначе
# один активный человек исчерпал бы лимит на всех).
_discussion_limiter = SlidingWindowLimiter(
    max_calls=config.discussion_max_replies_per_hour, window_seconds=3600
)

# R-CONVENIENCE: /pause останавливает АВТОматический постинг, не /postnow
# (ручной триггер остаётся доступен всегда — это осознанное действие
# админа, пауза не должна его блокировать). Не персистится между
# рестартами — простой ин-memory флажок для временной приостановки.
_posting_paused = False

# R-ROBUST: если публикация упала (сеть, бот потерял права в канале), не
# ждём весь POST_INTERVAL_HOURS до следующей попытки — это может стоить
# целых суток пропущенного поста из-за разовой ошибки.
_RETRY_DELAY_SECONDS = 30 * 60

# Черновик, ожидающий решения админа (см. ChannelBotConfig.require_approval).
# Не персистится между рестартами — если бот перезапустят с необработанным
# черновиком, он просто сгенерирует новый на следующем цикле, старая тема
# из очереди уже израсходована и это ок (не критичная потеря).
_pending_draft: GeneratedPost | None = None
# file_id фото, присланного админом к текущему черновику (см.
# handle_admin_photo) — публикуется вместе с текстом при апруве. Сбрасывается
# при каждом новом черновике (реролл/расписание), чтобы старое фото не
# случайно приклеилось к другому посту.
_pending_photo: str | None = None
# True между нажатием «✏️ Предложить правки» и следующим текстовым
# сообщением админа — следующее текстовое сообщение в админ-чате трактуется
# как правка к _pending_draft, а не игнорируется (см. handle_admin_feedback).
_awaiting_feedback: bool = False
# Тема слота (см. _DAILY_SLOTS), из которого получен текущий черновик — None
# для ручного /postnow и /preview без аргумента (свободный формат). Нужна,
# чтобы реролл («🔄 Новый пост») пересоздавал черновик С ТОЙ ЖЕ темой слота,
# а не случайным форматом.
_pending_slot_category: str | None = None
# Какой канал получит текущий черновик — основной (config.channel_id) или
# команда-канал (config.team_channel_id, см. /postnow team|/preview team|
# /draft team). По умолчанию основной; сбрасывается обратно к нему при
# approve/discard, чтобы следующий обычный /postnow не унаследовал случайно
# канал команды от предыдущего черновика.
_pending_channel_id: str = config.channel_id
_PENDING_DRAFT_RECHECK_SECONDS = 5 * 60

# /compose: посты, для которых нет источника фактов ни в очереди, ни в
# AI_CHANGELOG.md, ни в Docs/*.md (история выбора иконки, кто есть кто в
# команде) — бот собирает факты вопросами в личке ДО генерации черновика,
# вместо того чтобы придумывать/обобщать несуществующий контекст (см.
# content_generator.generate_clarifying_questions/generate_compose_post).
# "icon" → основной канал (config.channel_id), "team" → команда-канал
# (config.team_channel_id) — те же два целевых канала, что уже поддержаны
# для /postnow|/preview|/draft team, просто с другим способом получить
# исходные факты для черновика.
_COMPOSE_KIND_BY_TARGET = {"icon": "icon_story", "team": "team_roster"}
# Максимум раундов уточняющих вопросов — без этого добросовестная модель
# теоретически может уточнять бесконечно; после лимита черновик собирается
# из того, что есть, а не блокирует пользователя навсегда.
_COMPOSE_MAX_ROUNDS = 2


@dataclass
class _ComposeSession:
    kind: str
    channel_id: str
    qa_pairs: list[tuple[str, str]] = field(default_factory=list)
    pending_questions: list[str] = field(default_factory=list)
    round: int = 0


_compose_session: _ComposeSession | None = None
# Факты последнего завершённого /compose по каждому kind — НЕ очищается
# вместе с _compose_session. Нужно, чтобы "🔄 Новый пост" (реролл, см.
# cb_reroll_post → _request_approval → _generate_post_for_slot) мог
# перегенерировать черновик из ТЕХ ЖЕ собранных фактов, не переспрашивая
# вопросы заново, — в отличие от обычных категорий реролл здесь не может
# "просто взять следующую тему из очереди", источника фактов больше нигде
# нет. Не персистится на диск — переживает только текущий процесс, это
# ок (реролл сразу после /compose в той же сессии — единственный сценарий).
_compose_qa_by_kind: dict[str, list[tuple[str, str]]] = {}
_effective_require_approval = config.require_approval and bool(config.admin_chat_id)
if config.require_approval and not config.admin_chat_id:
    logger.warning("CHANNEL_REQUIRE_APPROVAL=1, но CHANNEL_ADMIN_CHAT_ID не задан — черновику некуда идти, постим как обычно")

_REPLY_SYSTEM_PROMPT = (
    "Ты — Кубышка, бот приложения для личных финансов, отвечаешь в чате обсуждения "
    "своего Telegram-канала. Тон тёплый, с характером, коротко (1-3 предложения). "
    "Никогда не стыди пользователя. Если не знаешь ответа — честно скажи, что "
    "передашь команде, не выдумывай.\n"
    "Пиши ТОЛЬКО на русском — ни одного английского слова и ни одной "
    "латинской буквы внутри русского слова (например, недопустимо "
    "'cheaper', 'командe' с латинской e). Если нужно живое слово — подбирай "
    "русский аналог, а не смешивай языки.\n"
    "ЖЁСТКОЕ ОГРАНИЧЕНИЕ ДЛИНЫ: ответ — НЕ БОЛЬШЕ 250 СИМВОЛОВ. Это жёсткий "
    "лимит, важнее \"1-3 предложений\" выше — если для соблюдения лимита "
    "нужно сократить до одного простого предложения, сокращай."
)


def _is_admin_chat(message: Message) -> bool:
    if not config.admin_chat_id:
        return True
    return str(message.chat.id) == config.admin_chat_id


def _admin_filter(message: Message) -> bool:
    # Фильтр на уровне регистрации, не проверка внутри тела хендлера — иначе
    # команда типа /queue, напечатанная кем-то в чате обсуждения (не в
    # админ-чате), «съедала» бы сообщение молча (Command() перехватывает его
    # первым по порядку регистрации) и до discussion-хендлера оно бы не
    # дошло, даже если содержало «?»/упоминание бота.
    return _is_admin_chat(message)


@dp.message(Command("id"))
async def cmd_id(message: Message) -> None:
    await message.answer(f"Chat ID: <code>{message.chat.id}</code>")


@dp.my_chat_member()
async def handle_my_chat_member(update: ChatMemberUpdated) -> None:
    # Раньше единственный способ узнать chat_id канала по инвайт-ссылке был
    # разово прочитать getUpdates ДО того, как какой-нибудь бот начнёт их
    # непрерывно вычитывать поллингом — теперь оба бота работают постоянно
    # живьём, так что тот трюк больше не работает (апдейт вычитывается и
    # пропадает из очереди раньше, чем его можно посмотреть вручную). Вместо
    # разгадывания по логам ("Update id=N is not handled", без payload) —
    # шлём детали сразу в админ-чат, как только статус бота в любом чате
    # меняется (например, его добавили админом в новый канал).
    chat = update.chat
    status = update.new_chat_member.status
    logger.info("my_chat_member: chat_id=%s title=%r type=%s status=%s", chat.id, chat.title, chat.type, status)
    if config.admin_chat_id:
        await bot.send_message(
            config.admin_chat_id,
            f"👋 Статус бота изменился: «{chat.title or chat.id}» (chat_id: <code>{chat.id}</code>, тип {chat.type}) → {status}",
        )


@dp.message(Command("addtopic"), F.func(_admin_filter))
async def cmd_addtopic(message: Message, command: CommandObject) -> None:
    topic = (command.args or "").strip()
    if not topic:
        await message.answer("Формат: /addtopic тема для следующего поста")
        return
    append_topic(_QUEUE_PATH, topic)
    await message.answer(f"✅ Добавил в очередь тем: «{topic}»")


@dp.message(Command("queue"), F.func(_admin_filter))
async def cmd_queue(message: Message) -> None:
    topics = load_queue(_QUEUE_PATH)
    if not topics:
        await message.answer("Очередь тем пуста — следующий пост возьмётся из AI_CHANGELOG.md или придумается сам.")
        return
    lines = [f"{i}. {t}" for i, t in enumerate(topics, start=1)]
    await message.answer("Очередь тем:\n" + "\n".join(lines) + "\n\nУдалить: /removetopic <номер>")


@dp.message(Command("removetopic"), F.func(_admin_filter))
async def cmd_removetopic(message: Message, command: CommandObject) -> None:
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Формат: /removetopic 2 (номер из /queue)")
        return
    topics = load_queue(_QUEUE_PATH)
    index = int(arg) - 1
    if not (0 <= index < len(topics)):
        await message.answer("Такого номера нет — посмотри /queue.")
        return
    removed = topics.pop(index)
    save_queue(_QUEUE_PATH, topics)
    await message.answer(f"🗑 Удалил из очереди: «{removed}»")


@dp.message(Command("addstory"), F.func(_admin_filter))
async def cmd_addstory(message: Message, command: CommandObject) -> None:
    # R-COST: отдельная очередь, не /addtopic — вечерний слот (см.
    # _DAILY_SLOTS) берёт тему ТОЛЬКО отсюда, чтобы не выдумывать личную
    # историю самостоятельно (см. generate_post_for_category).
    story = (command.args or "").strip()
    if not story:
        await message.answer("Формат: /addstory реальная история или наблюдение из жизни для личного поста")
        return
    append_topic(_STORY_QUEUE_PATH, story)
    await message.answer(f"✅ Добавил в очередь личных историй: «{story}»")


@dp.message(Command("stories"), F.func(_admin_filter))
async def cmd_stories(message: Message) -> None:
    stories = load_queue(_STORY_QUEUE_PATH)
    if not stories:
        await message.answer(
            "Очередь личных историй пуста — вечерний слот пока будет заменяться постом про фичу. Добавь через /addstory."
        )
        return
    lines = [f"{i}. {s}" for i, s in enumerate(stories, start=1)]
    await message.answer("Очередь личных историй:\n" + "\n".join(lines) + "\n\nУдалить: /removestory <номер>")


@dp.message(Command("removestory"), F.func(_admin_filter))
async def cmd_removestory(message: Message, command: CommandObject) -> None:
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Формат: /removestory 2 (номер из /stories)")
        return
    stories = load_queue(_STORY_QUEUE_PATH)
    index = int(arg) - 1
    if not (0 <= index < len(stories)):
        await message.answer("Такого номера нет — посмотри /stories.")
        return
    removed = stories.pop(index)
    save_queue(_STORY_QUEUE_PATH, stories)
    await message.answer(f"🗑 Удалил из очереди историй: «{removed}»")


@dp.message(Command("feedback"), F.func(_admin_filter))
async def cmd_feedback(message: Message, command: CommandObject) -> None:
    # R-CONVENIENCE: в отличие от кнопки "Предложить правки" (которая правит
    # ОДИН конкретный черновик), это можно отправить в любой момент, без
    # привязки к текущему посту — общее замечание по стилю на будущее.
    text = (command.args or "").strip()
    if not text:
        await message.answer("Формат: /feedback как хочешь, чтобы посты писались иначе — учту во всех следующих")
        return
    add_feedback(_FEEDBACK_PATH, text)
    await message.answer("✅ Запомнил, буду учитывать в следующих постах. /feedbacklist — посмотреть все.")


@dp.message(Command("feedbacklist"), F.func(_admin_filter))
async def cmd_feedbacklist(message: Message) -> None:
    notes = load_feedback(_FEEDBACK_PATH)
    if not notes:
        await message.answer("Замечаний пока нет — добавь через /feedback.")
        return
    lines = [f"{i}. {n}" for i, n in enumerate(notes, start=1)]
    await message.answer("Замечания, которые учитываются в постах:\n" + "\n".join(lines) + "\n\nУдалить: /removefeedback <номер>")


@dp.message(Command("removefeedback"), F.func(_admin_filter))
async def cmd_removefeedback(message: Message, command: CommandObject) -> None:
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Формат: /removefeedback 2 (номер из /feedbacklist)")
        return
    removed = remove_feedback(_FEEDBACK_PATH, int(arg) - 1)
    if removed is None:
        await message.answer("Такого номера нет — посмотри /feedbacklist.")
        return
    await message.answer(f"🗑 Удалил замечание: «{removed}»")


@dp.message(Command("status"), F.func(_admin_filter))
async def cmd_status(message: Message) -> None:
    topics_count = len(load_queue(_QUEUE_PATH))
    stories_count = len(load_queue(_STORY_QUEUE_PATH))
    feedback_count = len(load_feedback(_FEEDBACK_PATH))
    now = datetime.now(timezone.utc)
    next_slot_dt, next_category = _next_slot_datetime(now, skip_slot=_load_last_triggered_slot())
    next_slot_msk = next_slot_dt + timedelta(hours=_MSK_OFFSET_HOURS)
    override_at = _next_override_at()
    if override_at is not None and override_at < next_slot_dt:
        override_msk = override_at + timedelta(hours=_MSK_OFFSET_HOURS)
        pending_overrides = _load_slot_overrides()
        next_override = pending_overrides[0] if pending_overrides else {}
        fixed_text = next_override.get("text")
        # Показываем сам текст, если он зафиксирован (/overridepost с готовым
        # текстом) — без этого не видно, что реально уйдёт в апрув, только
        # факт "оверрайд есть" (см. живую путаницу — /draft генерирует
        # СЛУЧАЙНЫЙ черновик и вообще не трогает эту очередь).
        text_preview = f"\n«{fixed_text[:120]}{'…' if len(fixed_text) > 120 else ''}»" if fixed_text else ""
        override_line = f"🗓 Оверрайд в очереди — апрув/публикация в {override_msk:%d.%m %H:%M} МСК{text_preview}\n"
    else:
        override_line = ""
    lead_note = f" (черновик придёт за {_APPROVAL_LEAD_MINUTES} мин до этого)" if _effective_require_approval else ""
    last_info = load_last_post_info(_POST_STATE_PATH)
    if last_info:
        ago_hours = round((datetime.now(timezone.utc) - last_info["last_post_at"]).total_seconds() / 3600, 1)
        title = last_info["last_post_title"] or "(без заголовка)"
        last_line = f"📝 Последний пост: {ago_hours} ч назад — «{title}»"
    else:
        last_line = "📝 Постов ещё не было"
    if _effective_require_approval:
        draft_status = " — сейчас есть черновик, ждёт решения" if _pending_draft else ""
        photo_status = " (📎 фото прикреплено)" if _pending_photo else ""
        feedback_status = " (✏️ жду твою правку текстом)" if _awaiting_feedback else ""
        mode_line = f"📝 Режим: черновик на ревью{draft_status}{photo_status}{feedback_status}"
    else:
        mode_line = "🤖 Режим: полная автономность"
    await message.answer(
        f"{'⏸ Автопостинг на паузе (/resume)' if _posting_paused else '▶️ Автопостинг активен'}\n"
        f"{mode_line}\n"
        f"{last_line}\n"
        f"📋 Тем в очереди: {topics_count} | 📔 личных историй: {stories_count} | 🗒 замечаний: {feedback_count}\n"
        f"{override_line}"
        f"⏰ Следующий слот: {_CATEGORY_LABELS.get(next_category, next_category)} в {next_slot_msk:%H:%M} МСК{lead_note}\n"
        f"💬 Ответы в обсуждении: {'включены' if config.discussion_chat_id else 'выключены (DISCUSSION_CHAT_ID не задан)'}\n"
        f"👥 Команда-канал: {'настроен (/postnow team, /preview team, /draft team)' if config.team_channel_id else 'не настроен (TEAM_CHANNEL_ID не задан)'}"
    )


@dp.message(Command("pause"), F.func(_admin_filter))
async def cmd_pause(message: Message) -> None:
    global _posting_paused
    _posting_paused = True
    await message.answer("⏸ Автопостинг приостановлен. /postnow всё ещё работает вручную. /resume — включить обратно.")


@dp.message(Command("resume"), F.func(_admin_filter))
async def cmd_resume(message: Message) -> None:
    global _posting_paused
    _posting_paused = False
    await message.answer("▶️ Автопостинг снова включён.")


def _extract_title(post_text: str) -> str:
    return post_text.strip().splitlines()[0][:80] if post_text.strip() else ""


def _post_title(post: GeneratedPost) -> str:
    return post.question[:80] if post.kind == "poll" else _extract_title(post.text)


def _post_preview_text(post: GeneratedPost) -> str:
    if post.kind == "poll":
        options = "\n".join(f"▫️ {option}" for option in post.options)
        return f"📊 <b>Опрос</b>\n{post.question}\n\n{options}"
    return post.text


def _post_history_summary(post: GeneratedPost) -> str:
    """Короткая запись для post_history.py — то, что реальный будущий пост
    увидит в промпте как "уже сказанное", не полный текст."""
    if post.kind == "poll":
        return f"[опрос] {post.question}"
    return post.text[:220]


async def _send_generated_post(chat_id: str, post: GeneratedPost, photo: str | None = None) -> Message:
    if post.kind == "poll":
        # Telegram-опросы не поддерживают вложенное фото в том же сообщении —
        # если админ всё же прислал фото к черновику-опросу, тихо игнорируем
        # его здесь (handle_admin_photo уже предупреждает об этом при приёме).
        return await bot.send_poll(chat_id, post.question, post.options, is_anonymous=True)
    elif photo:
        return await bot.send_photo(chat_id, photo=photo, caption=post.text)
    else:
        return await bot.send_message(chat_id, post.text)


_SUBSCRIBER_SNAPSHOT_PATH = "channel_bot/subscriber_snapshot.json"


async def _fetch_subscriber_stats() -> tuple[int, int | None]:
    """Текущее число подписчиков канала (реальный вызов Bot API) и дельта к
    прошлому сохранённому снапшоту (None, если снапшота ещё не было). НЕ
    сохраняет новый снапшот сам — снапшот обновляется только в момент
    реальной публикации (см. _snapshot_if_feedback_metrics), иначе
    сгенерированный, но отклонённый/среролленный черновик испортил бы
    сравнение для следующего понедельника."""
    count = await bot.get_chat_member_count(config.channel_id)
    previous = load_subscriber_snapshot(_SUBSCRIBER_SNAPSHOT_PATH)
    delta = count - previous["count"] if previous else None
    return count, delta


async def _snapshot_if_feedback_metrics(category: str | None) -> None:
    if category != "feedback_metrics":
        return
    try:
        count = await bot.get_chat_member_count(config.channel_id)
        save_subscriber_snapshot(_SUBSCRIBER_SNAPSHOT_PATH, count, datetime.now(timezone.utc))
    except Exception:
        # Не мешает публикации — просто следующий понедельничный пост
        # покажет "нет данных о приросте" вместо реальной дельты.
        logger.exception("Failed to save subscriber snapshot after publishing feedback_metrics post")


async def _generate_post_for_slot(category: str | None, *, dry_run: bool = False) -> GeneratedPost:
    """category=None — свободный формат (ручной /postnow и /preview без
    аргумента). category="intro" — разовый закреплённый пост "кто мы", своя
    генерация без темы (см. generate_intro_post). category="feedback_metrics"
    — тоже особый случай (как "intro"): требует реального числа подписчиков
    из Bot API (get_chat_member_count), поэтому не идёт через
    generate_post_for_category (синхронный, без доступа к bot). Иначе — тема
    слота расписания (см. _WEEKLY_SLOTS); для category="personal" с пустой
    очередью историй и category="release_notes" без свежих записей changelog
    тихо подменяет на category="feature", а не выдумывает контент (см.
    generate_post_for_category)."""
    if category is None:
        return generate_next_post(
            llm,
            queue_path=_QUEUE_PATH,
            changelog_path=f"{config.finassist_docs_path}/AI_CHANGELOG.md",
            used_state_path=_CHANGELOG_STATE_PATH,
            docs_path=config.finassist_docs_path,
            dry_run=dry_run,
            beta_invite_url=config.beta_invite_url,
            feedback_path=_FEEDBACK_PATH,
            history_path=_HISTORY_PATH,
        )
    if category == "intro":
        return generate_intro_post(llm, config.finassist_docs_path, beta_invite_url=config.beta_invite_url, feedback_path=_FEEDBACK_PATH)
    if category in ("icon_story", "team_roster"):
        # Реролл (см. cb_reroll_post) или ручной /postnow|/preview с этой
        # категорией — регенерируем из ПОСЛЕДНИХ собранных через /compose
        # фактов (см. _compose_qa_by_kind), а не заново расспрашиваем: в
        # отличие от очереди/changelog, здесь взять "следующую тему" неоткуда.
        return generate_compose_post(llm, category, _compose_qa_by_kind.get(category, []), chat_context=_load_team_chat_context())
    if category == "feedback_metrics":
        count, delta = await _fetch_subscriber_stats()
        return generate_feedback_metrics_post(llm, count, delta, feedback_path=_FEEDBACK_PATH)
    post = generate_post_for_category(
        llm,
        category,
        queue_path=_QUEUE_PATH,
        story_queue_path=_STORY_QUEUE_PATH,
        changelog_path=f"{config.finassist_docs_path}/AI_CHANGELOG.md",
        used_state_path=_CHANGELOG_STATE_PATH,
        docs_path=config.finassist_docs_path,
        dry_run=dry_run,
        beta_invite_url=config.beta_invite_url,
        feedback_path=_FEEDBACK_PATH,
        history_path=_HISTORY_PATH,
    )
    if post is None:
        logger.info("'%s' slot had no available content — falling back to 'feature' category", category)
        return await _generate_post_for_slot("feature", dry_run=dry_run)
    return post


async def _pin_if_intro(category: str | None, sent: Message, channel_id: str) -> None:
    # R-CONVENIENCE: единственная категория, которую нужно закреплять — по
    # прямой просьбе ("это сообщение мы закрепим"). Требует прав админа с
    # can_pin_messages в канале — если их нет, публикация всё равно прошла,
    # только пин не сработал, поэтому не роняем публикацию из-за этого.
    # channel_id — канал, в который РЕАЛЬНО ушёл пост (основной или
    # команда-канал, см. _pending_channel_id) — раньше был захардкожен на
    # config.channel_id, что закрепляло бы не в том канале для team-черновика.
    if category != "intro":
        return
    try:
        await bot.pin_chat_message(channel_id, sent.message_id)
    except Exception:
        logger.exception("Failed to pin intro post — публикация прошла, но закрепить не получилось (нет прав у бота?)")


async def _publish_generated_post(
    category: str | None = None, *, fixed_post: GeneratedPost | None = None, channel_id: str | None = None
) -> None:
    channel_id = channel_id or config.channel_id
    post = fixed_post
    if post is None:
        sync_docs_repo(config.finassist_docs_path)
        post = await _generate_post_for_slot(category)
    sent = await _send_generated_post(channel_id, post)
    save_last_post_at(_POST_STATE_PATH, datetime.now(timezone.utc), title=_post_title(post))
    record_published_post(
        _HISTORY_PATH, category=category, summary=_post_history_summary(post), published_at=datetime.now(timezone.utc).isoformat()
    )
    await _pin_if_intro(category, sent, channel_id)
    await _snapshot_if_feedback_metrics(category)


def _resolve_command_target(raw_args: str) -> tuple[str, str] | None:
    """Общая разборка для /postnow, /preview, /draft: "team" первым словом →
    команда-канал, иначе основной. team без TEAM_CHANNEL_ID в .env → None
    (вызывающий код отвечает пользователю и не идёт дальше). Категория "team"
    без явной темы по умолчанию — "personal" (закулисные/личные истории
    команды, ровно то, для чего заводился этот канал), а не общий
    произвольный формат, как для основного канала без темы."""
    target, category_arg = _parse_target(raw_args)
    channel_id = _target_channel_id(target)
    category = category_arg.strip().lower() or None
    if target == "team" and category is None:
        category = "personal"
    return (channel_id, category) if channel_id else None


@dp.message(Command("postnow"), F.func(_admin_filter))
async def cmd_postnow(message: Message, command: CommandObject) -> None:
    resolved = _resolve_command_target(command.args or "")
    if resolved is None:
        await message.answer("TEAM_CHANNEL_ID не задан в .env — команда-канал ещё не настроен.")
        return
    channel_id, category = resolved
    if category and category not in _CATEGORY_LABELS:
        await message.answer(f"Неизвестная тема «{category}». Форматы: /postnow [team] [{_CATEGORY_ARG_HINT}]")
        return
    await message.answer("Публикую…")
    try:
        await _publish_generated_post(category=category, channel_id=channel_id)
    except Exception:
        logger.exception("Manual post failed")
        await message.answer("⚠️ Не получилось опубликовать — смотри логи бота.")
        return
    await message.answer("✅ Опубликовано.")


@dp.message(Command("preview"), F.func(_admin_filter))
async def cmd_preview(message: Message, command: CommandObject) -> None:
    resolved = _resolve_command_target(command.args or "")
    if resolved is None:
        await message.answer("TEAM_CHANNEL_ID не задан в .env — команда-канал ещё не настроен.")
        return
    _channel_id, category = resolved
    if category and category not in _CATEGORY_LABELS:
        await message.answer(f"Неизвестная тема «{category}». Форматы: /preview [team] [{_CATEGORY_ARG_HINT}]")
        return
    await message.answer("Генерирую предпросмотр (очередь/changelog не тронуты)…")
    try:
        sync_docs_repo(config.finassist_docs_path)
        preview_post = await _generate_post_for_slot(category, dry_run=True)
    except Exception:
        logger.exception("Preview generation failed")
        await message.answer("⚠️ Не получилось сгенерировать предпросмотр.")
        return
    await message.answer(f"👀 <b>Предпросмотр следующего поста:</b>\n\n{_post_preview_text(preview_post)}")


@dp.message(Command("draft"), F.func(_admin_filter))
async def cmd_draft(message: Message, command: CommandObject) -> None:
    # R-CONVENIENCE: в отличие от /postnow (публикует НЕМЕДЛЕННО), это
    # запрашивает черновик через обычный флоу ревью (кнопки апрув/реролл/
    # правки) вне расписания — нужно, например, чтобы вечером спокойно
    # довести до ума разовый intro-пост, а не ждать его слота в расписании
    # (у intro и вовсе нет слота в _WEEKLY_SLOTS, и у команда-канала нет
    # слотов вообще — сознательно не автопостится).
    resolved = _resolve_command_target(command.args or "")
    if resolved is None:
        await message.answer("TEAM_CHANNEL_ID не задан в .env — команда-канал ещё не настроен.")
        return
    channel_id, category = resolved
    if category and category not in _CATEGORY_LABELS:
        await message.answer(f"Неизвестная тема «{category}». Форматы: /draft [team] [{_CATEGORY_ARG_HINT}]")
        return
    if not config.admin_chat_id:
        await message.answer("Нужно задать CHANNEL_ADMIN_CHAT_ID, чтобы черновик было куда прислать.")
        return
    if _pending_draft is not None:
        await message.answer("Уже есть черновик, ждущий решения, — реши его сначала (кнопки выше).")
        return
    await message.answer("Генерирую черновик…")
    try:
        await _request_approval(category=category, channel_id=channel_id)
    except Exception:
        logger.exception("Manual draft request failed")
        await message.answer("⚠️ Не получилось сгенерировать черновик — смотри логи.")


@dp.message(Command("rawdraft"), F.func(_admin_filter))
async def cmd_rawdraft(message: Message, command: CommandObject) -> None:
    # R-CONVENIENCE: в отличие от /draft (текст всегда генерирует LLM), это —
    # готовый текст, который команда сама сформулировала и хочет опубликовать
    # буквально как есть, без переписывания. Идёт через ТОТ ЖЕ флоу апрув/
    # фото/публикация (_request_approval с fixed_post), просто без генерации.
    target, text = _parse_target(command.args or "")
    text = text.strip()
    if not text:
        await message.answer("Формат: /rawdraft [team] <готовый текст поста целиком>")
        return
    channel_id = _target_channel_id(target)
    if not channel_id:
        await message.answer("TEAM_CHANNEL_ID не задан в .env — команда-канал ещё не настроен.")
        return
    if _pending_draft is not None:
        await message.answer("Уже есть черновик, ждущий решения, — реши его сначала (кнопки выше).")
        return
    await _request_approval(category=None, fixed_post=GeneratedPost(kind="text", text=text), channel_id=channel_id)


def _format_questions_message(questions: list[str], *, first_round: bool) -> str:
    numbered = "\n".join(f"{i}. {q}" for i, q in enumerate(questions, start=1))
    intro = "Чтобы написать хороший пост, ответь, пожалуйста, одним сообщением на всё:" if first_round else "Уточню ещё немного:"
    return f"{intro}\n\n{numbered}\n\n(можно одним сообщением, не обязательно по пунктам)"


def _load_team_chat_context() -> str:
    """team_bot — единственный, кто реально состоит в командном чате и
    пишет в этот лог (см. shared/chat_log.py, TEAM_CHAT_ID); channel_bot сам
    не участник того чата, поэтому читает тот же файл, чтобы иметь тот же
    контекст для /compose. Пусто, если лога ещё нет (TEAM_CHAT_ID только
    что включили и сообщений ещё не накопилось, или файл не создан) —
    вызывающий код это переживает нормально (см. _format_qa_history)."""
    return format_for_prompt(all_messages(DEFAULT_LOG_PATH))


async def _start_compose_session(kind: str, channel_id: str) -> None:
    global _compose_session
    chat_context = _load_team_chat_context()
    questions = generate_clarifying_questions(llm, kind, qa_pairs=[], chat_context=chat_context)
    _compose_session = _ComposeSession(kind=kind, channel_id=channel_id, pending_questions=questions)
    if questions:
        await bot.send_message(config.admin_chat_id, _format_questions_message(questions, first_round=True))
    else:
        # Модель сразу решила, что вопросов не нужно (маловероятно на первом
        # раунде без единого факта, но fail-open — не блокируем странным
        # ответом модели) — сразу переходим к черновику с пустыми фактами.
        await _finish_compose_session()


async def _finish_compose_session() -> None:
    """Черновик готов (модель сочла фактов достаточно ИЛИ исчерпан
    _COMPOSE_MAX_ROUNDS) — генерируем пост и пускаем через ОБЫЧНЫЙ флоу
    ревью/правок/публикации (_request_approval), а не отдельный путь, чтобы
    не дублировать approve/reroll/discard/photo-attach логику."""
    global _compose_session
    session = _compose_session
    _compose_session = None
    _compose_qa_by_kind[session.kind] = session.qa_pairs
    await bot.send_message(config.admin_chat_id, "Собрал достаточно — готовлю черновик…")
    try:
        post = generate_compose_post(llm, session.kind, session.qa_pairs, chat_context=_load_team_chat_context())
        await _request_approval(category=session.kind, fixed_post=post, channel_id=session.channel_id)
    except Exception:
        logger.exception("Failed to generate compose draft")
        await bot.send_message(config.admin_chat_id, "⚠️ Не получилось собрать черновик — смотри логи бота.")


@dp.message(Command("compose"), F.func(_admin_filter))
async def cmd_compose(message: Message, command: CommandObject) -> None:
    target = (command.args or "").strip().lower()
    kind = _COMPOSE_KIND_BY_TARGET.get(target)
    if kind is None:
        await message.answer("Формат: /compose icon (пост про выбор иконки) или /compose team (кто мы, команда-канал)")
        return
    channel_id = config.team_channel_id if target == "team" else config.channel_id
    if not channel_id:
        await message.answer("TEAM_CHANNEL_ID не задан в .env — команда-канал ещё не настроен.")
        return
    if not config.admin_chat_id:
        await message.answer("Нужно задать CHANNEL_ADMIN_CHAT_ID, чтобы было куда слать вопросы и черновик.")
        return
    if _pending_draft is not None:
        await message.answer("Уже есть черновик, ждущий решения, — реши его сначала (кнопки выше).")
        return
    if _compose_session is not None:
        await message.answer("Уже идёт сбор фактов для другого поста — сначала ответь на те вопросы или дождись черновика.")
        return
    await message.answer(f"Начинаю: {_CATEGORY_LABELS[kind]}. Секунду…")
    try:
        await _start_compose_session(kind, channel_id)
    except Exception:
        logger.exception("Failed to start compose session")
        await message.answer("⚠️ Не получилось начать — смотри логи бота.")


@dp.message(Command("overridepost"), F.func(_admin_filter))
async def cmd_overridepost(message: Message, command: CommandObject) -> None:
    # R-CONVENIENCE: разово подменить конкретный слот расписания (например,
    # интро-пост вместо обычного поста в конкретный день) — не трогая
    # _WEEKLY_SLOTS насовсем. Время — момент самого ПОСТА (не апрува), лид-тайм
    # (_APPROVAL_LEAD_MINUTES) вычитается автоматически, как и для обычного
    # грид-расписания — админу не нужно считать его вручную.
    parts = (command.args or "").split()
    if len(parts) != 3:
        await message.answer(
            f"Формат: /overridepost 2026-07-18 10:00 intro\n"
            f"Дата и время — МСК, время самого поста (не апрува). Темы: {_CATEGORY_ARG_HINT}\n"
            "Ответь этой командой на сообщение с готовым текстом поста — тогда захардкодит именно этот "
            "текст (без генерации LLM), category нужен только для пометки в апруве."
        )
        return
    date_str, time_str, category = parts
    fixed_text = None
    if message.reply_to_message:
        fixed_text = (message.reply_to_message.text or message.reply_to_message.caption or "").strip() or None
    category = category.lower()
    if category not in _CATEGORY_LABELS:
        await message.answer(f"Неизвестная тема «{category}». Форматы: {_CATEGORY_ARG_HINT}")
        return
    try:
        post_at_msk = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    except ValueError:
        await message.answer("Не разобрал дату/время. Формат: /overridepost 2026-07-18 10:00 intro")
        return
    post_at_utc = post_at_msk.replace(tzinfo=timezone.utc) - timedelta(hours=_MSK_OFFSET_HOURS)
    if _effective_require_approval:
        trigger_at_utc = post_at_utc - timedelta(minutes=_APPROVAL_LEAD_MINUTES)
        approval_msk = post_at_msk - timedelta(minutes=_APPROVAL_LEAD_MINUTES)
        lead_note = f" (черновик на апрув придёт в {approval_msk:%H:%M} МСК)"
    else:
        trigger_at_utc = post_at_utc
        lead_note = ""
    _add_slot_override(trigger_at_utc, category, text=fixed_text)
    text_note = " — с фиксированным текстом" if fixed_text else ""
    await message.answer(f"✅ На {date_str} {time_str} МСК запланирован пост: {_CATEGORY_LABELS[category]}{text_note}{lead_note}")


def _approval_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Опубликовать", callback_data="approve_post"),
                InlineKeyboardButton(text="🗑 Пропустить", callback_data="reject_post"),
            ]
        ]
    )


def _reject_followup_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔄 Новый пост", callback_data="reroll_post"),
                InlineKeyboardButton(text="✏️ Предложить правки", callback_data="suggest_edit"),
            ],
            [InlineKeyboardButton(text="🚫 Не публиковать вообще", callback_data="discard_post")],
        ]
    )


async def _request_approval(
    category: str | None = None, *, fixed_post: GeneratedPost | None = None, channel_id: str | None = None
) -> None:
    global _pending_draft, _pending_photo, _awaiting_feedback, _pending_slot_category, _pending_channel_id
    post = fixed_post
    if post is None:
        sync_docs_repo(config.finassist_docs_path)
        post = await _generate_post_for_slot(category)
    _pending_draft = post
    _pending_photo = None
    _awaiting_feedback = False
    _pending_slot_category = category
    _pending_channel_id = channel_id or config.channel_id
    category_hint = f" ({_CATEGORY_LABELS[category]})" if category else ""
    target_hint = " · команда-канал" if _pending_channel_id == config.team_channel_id else ""
    photo_hint = "\n\n📎 Пришли мне фото, если хочешь прикрепить его к посту." if post.kind == "text" else ""
    await bot.send_message(
        config.admin_chat_id,
        f"👀 <b>Черновик поста{category_hint}{target_hint} — нужно решение:</b>\n\n{_post_preview_text(post)}{photo_hint}",
        reply_markup=_approval_keyboard(),
    )


def _is_admin_callback(callback: CallbackQuery) -> bool:
    return bool(callback.message) and str(callback.message.chat.id) == config.admin_chat_id


@dp.message(F.photo, F.func(_is_admin_chat))
async def handle_admin_photo(message: Message) -> None:
    global _pending_photo
    if _pending_draft is None:
        await message.reply("Сейчас нет черновика, к которому можно прикрепить фото.")
        return
    if _pending_draft.kind == "poll":
        await message.reply("К опросу нельзя прикрепить фото — это ограничение Telegram-опросов.")
        return
    # message.photo — несколько размеров одного фото, последний — самый крупный.
    _pending_photo = message.photo[-1].file_id
    await message.reply("📎 Фото прикреплено к черновику. Нажми «Опубликовать», когда будешь готов.")


@dp.callback_query(F.data == "approve_post")
async def cb_approve_post(callback: CallbackQuery) -> None:
    global _pending_draft, _pending_photo, _pending_slot_category, _pending_channel_id
    if not _is_admin_callback(callback):
        await callback.answer("Только из админ-чата")
        return
    if _pending_draft is None:
        await callback.answer("Черновика уже нет — возможно, кто-то уже решил.")
        return
    post = _pending_draft
    photo = _pending_photo
    category = _pending_slot_category
    channel_id = _pending_channel_id
    _pending_draft = None
    _pending_photo = None
    _pending_slot_category = None
    _pending_channel_id = config.channel_id
    try:
        sent = await _send_generated_post(channel_id, post, photo=photo)
        save_last_post_at(_POST_STATE_PATH, datetime.now(timezone.utc), title=_post_title(post))
        record_published_post(
            _HISTORY_PATH, category=category, summary=_post_history_summary(post), published_at=datetime.now(timezone.utc).isoformat()
        )
        await _pin_if_intro(category, sent, channel_id)
        await _snapshot_if_feedback_metrics(category)
    except Exception:
        logger.exception("Failed to publish approved draft")
        await callback.answer("⚠️ Не получилось опубликовать — смотри логи.", show_alert=True)
        return
    if callback.message:
        await callback.message.edit_text(f"✅ Опубликовано{' (с фото)' if photo else ''}:\n\n{_post_preview_text(post)}")
    await callback.answer("Опубликовано")


@dp.callback_query(F.data == "reject_post")
async def cb_reject_post(callback: CallbackQuery) -> None:
    global _awaiting_feedback
    if not _is_admin_callback(callback):
        await callback.answer("Только из админ-чата")
        return
    if _pending_draft is None:
        await callback.answer("Черновика уже нет — возможно, кто-то уже решил.")
        return
    # R-CONVENIENCE: по просьбе — не выбрасывать черновик молча и не решать
    # за админа между "новый" и "поправить" — черновик остаётся в
    # _pending_draft, чтобы кнопка «Предложить правки» могла его переписать.
    _awaiting_feedback = False
    if callback.message:
        await callback.message.edit_text(
            f"🗑 Ок, этот вариант не публикуем. Что дальше?\n\n{_post_preview_text(_pending_draft)}",
            reply_markup=_reject_followup_keyboard(),
        )
    await callback.answer("Выбери, что делать дальше")


@dp.callback_query(F.data == "reroll_post")
async def cb_reroll_post(callback: CallbackQuery) -> None:
    global _pending_draft, _pending_photo, _awaiting_feedback
    if not _is_admin_callback(callback):
        await callback.answer("Только из админ-чата")
        return
    # Реролл сохраняет тему СЛОТА (см. _pending_slot_category) и ЦЕЛЕВОЙ
    # канал (см. _pending_channel_id) — если это был team-черновик, новый
    # вариант тоже должен уйти на апрув для команда-канала, а не молча
    # откатиться на основной.
    category = _pending_slot_category
    channel_id = _pending_channel_id
    _pending_draft = None
    _pending_photo = None
    _awaiting_feedback = False
    if callback.message:
        await callback.message.edit_text("🔄 Черновик пропущен — готовлю другой вариант…")
    await callback.answer("Готовлю новый вариант")
    try:
        await _request_approval(category=category, channel_id=channel_id)
    except Exception:
        logger.exception("Failed to generate replacement draft after reroll")
        await bot.send_message(
            config.admin_chat_id, "⚠️ Не получилось сгенерировать новый вариант — попробуй /postnow позже или проверь логи."
        )


@dp.callback_query(F.data == "discard_post")
async def cb_discard_post(callback: CallbackQuery) -> None:
    # R-ROBUST: без этой кнопки любой отклонённый черновик рано или поздно
    # заменяется НОВЫМ (реролл/правки) — свободного слота "ничего не решено,
    # просто пусто" не было вообще. Это реально блокировало расписание:
    # неразрешённый _pending_draft держит планировщик в ожидании (см.
    # post_scheduled_content), включая разовые оверрайды типа интро-поста —
    # поймано вживую (см. память проекта), когда черновик из ручного /draft
    # завис бы и не пустил апрув интро-поста на следующее утро.
    global _pending_draft, _pending_photo, _awaiting_feedback, _pending_slot_category, _pending_channel_id
    if not _is_admin_callback(callback):
        await callback.answer("Только из админ-чата")
        return
    _pending_draft = None
    _pending_photo = None
    _awaiting_feedback = False
    _pending_slot_category = None
    _pending_channel_id = config.channel_id
    if callback.message:
        await callback.message.edit_text("🚫 Черновик отклонён, без замены. Следующий — по расписанию.")
    await callback.answer("Ок, не публикуем")


@dp.callback_query(F.data == "suggest_edit")
async def cb_suggest_edit(callback: CallbackQuery) -> None:
    global _awaiting_feedback
    if not _is_admin_callback(callback):
        await callback.answer("Только из админ-чата")
        return
    if _pending_draft is None:
        await callback.answer("Черновика уже нет — возможно, кто-то уже решил.")
        return
    if _pending_draft.kind == "poll":
        # Свободная правка текстом плохо определена для опроса (вопрос +
        # варианты, а не связный текст) — проще перегенерировать целиком.
        await callback.answer("Правки пока доступны только для текстовых постов — жми «Новый пост».", show_alert=True)
        return
    _awaiting_feedback = True
    if callback.message:
        await callback.message.edit_text(
            "✏️ Напиши, что поправить или какую идею добавить, — перепишу пост с учётом этого.\n\n"
            f"Черновик, который правим:\n\n{_post_preview_text(_pending_draft)}"
        )
    await callback.answer("Жду твою правку текстом")


@dp.message(F.text, F.func(_is_admin_chat))
async def handle_admin_feedback(message: Message) -> None:
    global _pending_draft, _awaiting_feedback, _compose_session
    # Сбор фактов для /compose проверяем ПЕРВЫМ — это тот же "единственный
    # обработчик произвольного текста в админ-чате, разруливает по
    # состоянию внутри тела", что и режим правки ниже, оба состояния взаимно
    # исключают друг друга (см. проверку "уже есть черновик"/"уже идёт сбор
    # фактов" в cmd_compose и cmd_draft/postnow/preview).
    if _compose_session is not None:
        answer = (message.text or "").strip()
        if not answer:
            return
        session = _compose_session
        questions_text = "\n".join(session.pending_questions)
        session.qa_pairs.append((questions_text, answer))
        session.round += 1
        if session.round >= _COMPOSE_MAX_ROUNDS:
            await _finish_compose_session()
            return
        await bot.send_chat_action(message.chat.id, "typing")
        try:
            more_questions = generate_clarifying_questions(
                llm, session.kind, session.qa_pairs, chat_context=_load_team_chat_context()
            )
        except Exception:
            logger.exception("Failed to get follow-up compose questions — proceeding to draft with what we have")
            more_questions = []
        if not more_questions:
            await _finish_compose_session()
            return
        session.pending_questions = more_questions
        await message.answer(_format_questions_message(more_questions, first_round=False))
        return
    if not _awaiting_feedback or _pending_draft is None:
        # Не режим правки — это не наш хендлер, молча пропускаем (в
        # админ-чате нет другого обработчика произвольного текста).
        return
    feedback = (message.text or "").strip()
    if not feedback:
        return
    _awaiting_feedback = False
    await bot.send_chat_action(message.chat.id, "typing")
    try:
        revised = revise_post(llm, _pending_draft, feedback, feedback_path=_FEEDBACK_PATH, category=_pending_slot_category)
    except Exception:
        logger.exception("Failed to revise draft from admin feedback")
        await message.reply("⚠️ Не получилось переписать пост — попробуй ещё раз или жми «Новый пост» после следующего /postnow.")
        return
    # R-CONVENIENCE: правка из этой кнопки не только чинит ТЕКУЩИЙ черновик —
    # она же навсегда попадает в общий фидбек-лог (см. feedback_store.py),
    # чтобы влиять на ВСЕ последующие посты, а не только на этот один (по
    # прямой просьбе — "бот должен учиться на правках").
    add_feedback(_FEEDBACK_PATH, feedback)
    _pending_draft = revised
    await message.reply(
        f"✏️ <b>Переписал с учётом правки</b> (и запомнил её на будущее):\n\n{_post_preview_text(revised)}",
        reply_markup=_approval_keyboard(),
    )


async def post_scheduled_content() -> None:
    while True:
        if _effective_require_approval and _pending_draft is not None:
            # Уже ждём решения по текущему черновику — не начинаем новый цикл
            # генерации поверх него.
            await asyncio.sleep(_PENDING_DRAFT_RECHECK_SECONDS)
            continue
        if _posting_paused:
            await asyncio.sleep(60)
            continue
        now = datetime.now(timezone.utc)

        # Разовые оверрайды (см. /overridepost) идут ПЕРЕД обычным гридом —
        # например, интро-пост вместо обычного слота в конкретный день.
        due_override = _pop_due_override(now)
        if due_override is not None:
            fixed_text = due_override.get("text")
            fixed_post = GeneratedPost(kind="text", text=fixed_text) if fixed_text else None
            try:
                if _effective_require_approval:
                    await _request_approval(category=due_override["category"], fixed_post=fixed_post)
                else:
                    await _publish_generated_post(category=due_override["category"], fixed_post=fixed_post)
            except Exception:
                logger.exception("Failed to handle slot override")
                await asyncio.sleep(_RETRY_DELAY_SECONDS)
            continue

        # R-CONVENIENCE: расписание чисто по времени суток (_DAILY_SLOTS), не
        # "N часов после последнего поста" — если бот перезапустили и слот
        # уже прошёл, он просто пропускается (не постим задним числом), тот
        # же принцип, что раньше давал избежать спама после рестарта.
        last_triggered = _load_last_triggered_slot()
        slot_dt, category = _next_slot_datetime(now, skip_slot=last_triggered)

        next_override_at = _next_override_at()
        if next_override_at is not None and next_override_at < slot_dt:
            # Ближайший оверрайд наступит раньше обычного слота — ждём его,
            # а не обычный грид.
            wait_seconds = (next_override_at - now).total_seconds()
            await asyncio.sleep(max(1.0, min(wait_seconds, _PENDING_DRAFT_RECHECK_SECONDS)))
            continue

        # По просьбе — черновик на апрув приходит за _APPROVAL_LEAD_MINUTES
        # до целевого времени поста, не ровно в момент поста (в автономном
        # режиме смотреть черновик некому — публикуем ровно в слот).
        trigger_dt = slot_dt - timedelta(minutes=_APPROVAL_LEAD_MINUTES) if _effective_require_approval else slot_dt
        wait_seconds = (trigger_dt - now).total_seconds()
        if wait_seconds > 0:
            await asyncio.sleep(min(wait_seconds, _PENDING_DRAFT_RECHECK_SECONDS))
            continue
        try:
            if _effective_require_approval:
                await _request_approval(category=category)
                # last_post_at обновится при апруве (cb_approve_post) — тут
                # не спим долго, следующая итерация быстро увидит pending и
                # уйдёт в ветку выше.
            else:
                await _publish_generated_post(category=category)
            # Отмечаем слот обработанным СРАЗУ после запроса апрува (не после
            # решения админа) — иначе если админ решит рано (в пределах
            # 30-минутного окна до slot_dt), следующая итерация цикла увидит
            # тот же slot_dt всё ещё в будущем и запросит апрув повторно.
            _save_last_triggered_slot(slot_dt)
        except Exception:
            logger.exception("Failed to publish scheduled post")
            await asyncio.sleep(_RETRY_DELAY_SECONDS)


def _should_reply(text: str) -> bool:
    # R1: полная автономность в СВОЁМ чате обсуждения — но отвечать буквально
    # на каждую реплику в живом community-чате шумно и выглядит навязчиво.
    # Эвристика: явный вопрос или обращение к боту. Если станет мало —
    # ослабить/убрать условие, это сознательный, легко настраиваемый выбор.
    return "?" in text or "кубышк" in text.lower()


async def handle_discussion_message(message: Message) -> None:
    text = message.text or ""
    if not text or not _should_reply(text):
        return
    if message.from_user and message.from_user.is_bot:
        # Защита от зацикливания: если в чате обсуждения есть другой бот
        # (например, модераторский), два бота, отвечающие друг другу, могли
        # бы уйти в бесконечный цикл — каждый круг стоит реальных денег.
        return
    user_key = message.from_user.id if message.from_user else message.chat.id
    if not _discussion_limiter.allow(user_key):
        # Молча пропускаем, не отвечаем "лимит исчерпан" — в отличие от
        # team_bot, это публичный чат: объяснять незнакомым людям про
        # внутренний rate-limit неуместно, просто не подключаемся к разговору.
        return
    await bot.send_chat_action(message.chat.id, "typing")
    context = load_project_context(config.finassist_docs_path, max_chars=4000)
    answer = llm.chat(
        [
            {"role": "system", "content": f"{_REPLY_SYSTEM_PROMPT}\n\nКонтекст проекта:\n{context}"},
            {"role": "user", "content": text},
        ],
        # R-COST: LLMConfig.reasoning_effort="minimal" убирает налог на
        # скрытые reasoning-токены (см. content_generator.py/team_bot/main.py) —
        # без него 250 не хватало, с "minimal" хватает с запасом.
        max_tokens=300,
    )
    if answer:
        await message.reply(answer)


def _register_discussion_handler() -> None:
    # discussion_chat_id опционален: без него канал только постит, без ответов
    # в комментариях (например, пока обсуждение ещё не подключено к каналу).
    if not config.discussion_chat_id:
        logger.info("DISCUSSION_CHAT_ID не задан — ответы в комментариях выключены.")
        return
    dp.message(F.chat.id == int(config.discussion_chat_id))(handle_discussion_message)


_DEFAULT_COMMANDS = [BotCommand(command="id", description="ID текущего чата")]
_ADMIN_COMMANDS = _DEFAULT_COMMANDS + [
    BotCommand(command="addtopic", description="Добавить тему в очередь"),
    BotCommand(command="queue", description="Очередь тем"),
    BotCommand(command="removetopic", description="Удалить тему из очереди"),
    BotCommand(command="addstory", description="Добавить личную историю в очередь"),
    BotCommand(command="stories", description="Очередь личных историй"),
    BotCommand(command="removestory", description="Удалить историю из очереди"),
    BotCommand(command="feedback", description="Замечание по стилю — учту в следующих постах"),
    BotCommand(command="feedbacklist", description="Список замечаний"),
    BotCommand(command="removefeedback", description="Удалить замечание"),
    BotCommand(command="draft", description="Черновик на ревью [team] [poll/feature/personal/feedback_metrics/release_notes/intro]"),
    BotCommand(command="rawdraft", description="Черновик из готового текста [team] <текст> — без LLM, как есть"),
    BotCommand(command="compose", description="Собрать факты вопросами и написать пост: icon (иконка) или team (кто мы)"),
    BotCommand(command="overridepost", description="Разово подменить пост в расписании (дата время тема)"),
    BotCommand(command="preview", description="Предпросмотр [team] [poll/feature/personal/feedback_metrics/release_notes/intro]"),
    BotCommand(command="postnow", description="Опубликовать сейчас [team] [poll/feature/personal/feedback_metrics/release_notes/intro]"),
    BotCommand(command="pause", description="Приостановить автопостинг"),
    BotCommand(command="resume", description="Возобновить автопостинг"),
    BotCommand(command="status", description="Статус бота"),
]


async def _register_bot_commands() -> None:
    # Админ-команды видны (автодополнением "/") только в админ-чате, если он
    # задан — остальным чатам ни к чему видеть команды, которые всё равно
    # отклонит _admin_filter. Без CHANNEL_ADMIN_CHAT_ID (локальная разработка)
    # показываем весь список везде, как и раньше было доступно везде.
    if config.admin_chat_id:
        await bot.set_my_commands(_DEFAULT_COMMANDS)
        try:
            await bot.set_my_commands(_ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=int(config.admin_chat_id)))
        except (ValueError, TypeError):
            logger.warning("CHANNEL_ADMIN_CHAT_ID=%r не похож на числовой chat_id — команды не отображены там отдельно", config.admin_chat_id)
    else:
        await bot.set_my_commands(_ADMIN_COMMANDS)


async def main() -> None:
    _register_discussion_handler()
    await _register_bot_commands()
    asyncio.create_task(post_scheduled_content())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
