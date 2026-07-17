from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import BotCommand, BotCommandScopeChat, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from channel_bot.content_generator import GeneratedPost, generate_intro_post, generate_next_post, generate_post_for_category, revise_post
from channel_bot.content_queue import append_topic, load_queue, save_queue
from channel_bot.feedback_store import add_feedback, load_feedback, remove_feedback
from channel_bot.post_history import record_published_post
from channel_bot.post_state import load_last_post_info, save_last_post_at
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

# По просьбе — 2-3 поста в день с ЗАКРЕПЛЁННОЙ темой на слот вместо случайного
# формата каждый раз (было: 1 пост/сутки, формат — рулетка). Время — МСК,
# час дня выбран по общей практике для лайфстайл/финансового контента (утро —
# лёгкое вовлечение, обед — основной продуктовый контент, вечер — самое
# активное время для реакций/комментариев). Список отсортирован по времени
# суток — см. _next_slot_datetime, порядок важен.
_DAILY_SLOTS: list[tuple[int, int, str]] = [
    (10, 0, "poll"),
    (14, 30, "feature"),
    (19, 30, "personal"),
]
_CATEGORY_LABELS = {"poll": "опрос", "feature": "про фичу", "personal": "личная история", "intro": "закреплённый интро-пост"}
# Россия не переходит на летнее время — фиксированный оффсет ок, без tzdata.
_MSK_OFFSET_HOURS = 3


def _next_slot_datetime(now_utc: datetime) -> tuple[datetime, str]:
    """Ближайший следующий слот (строго после now_utc) из _DAILY_SLOTS —
    сегодняшний, если ещё не наступил, иначе первый слот следующих суток."""
    for day_offset in (0, 1):
        day = now_utc + timedelta(days=day_offset)
        for hour_msk, minute_msk, category in _DAILY_SLOTS:
            slot_dt = day.replace(hour=hour_msk - _MSK_OFFSET_HOURS, minute=minute_msk, second=0, microsecond=0)
            if day_offset == 0 and slot_dt <= now_utc:
                continue
            return slot_dt, category
    raise AssertionError("unreachable — _DAILY_SLOTS должен быть непустым")

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
_PENDING_DRAFT_RECHECK_SECONDS = 5 * 60
_effective_require_approval = config.require_approval and bool(config.admin_chat_id)
if config.require_approval and not config.admin_chat_id:
    logger.warning("CHANNEL_REQUIRE_APPROVAL=1, но CHANNEL_ADMIN_CHAT_ID не задан — черновику некуда идти, постим как обычно")

_REPLY_SYSTEM_PROMPT = (
    "Ты — Кубышка, бот приложения для личных финансов, отвечаешь в чате обсуждения "
    "своего Telegram-канала. Тон тёплый, с характером, коротко (1-3 предложения). "
    "Никогда не стыди пользователя. Если не знаешь ответа — честно скажи, что "
    "передашь команде, не выдумывай."
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
    next_slot_dt, next_category = _next_slot_datetime(datetime.now(timezone.utc))
    next_slot_msk = next_slot_dt + timedelta(hours=_MSK_OFFSET_HOURS)
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
        f"⏰ Следующий слот: {_CATEGORY_LABELS.get(next_category, next_category)} в {next_slot_msk:%H:%M} МСК\n"
        f"💬 Ответы в обсуждении: {'включены' if config.discussion_chat_id else 'выключены (DISCUSSION_CHAT_ID не задан)'}"
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


def _generate_post_for_slot(category: str | None, *, dry_run: bool = False) -> GeneratedPost:
    """category=None — свободный формат (ручной /postnow и /preview без
    аргумента). category="intro" — разовый закреплённый пост "кто мы", своя
    генерация без темы (см. generate_intro_post). Иначе — тема слота
    расписания (см. _DAILY_SLOTS); для category="personal" с пустой
    очередью историй тихо подменяет на category="feature", а не выдумывает
    историю (см. generate_post_for_category)."""
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
        logger.info("Story queue empty for 'personal' slot — falling back to 'feature' category")
        return _generate_post_for_slot("feature", dry_run=dry_run)
    return post


async def _pin_if_intro(category: str | None, sent: Message) -> None:
    # R-CONVENIENCE: единственная категория, которую нужно закреплять — по
    # прямой просьбе ("это сообщение мы закрепим"). Требует прав админа с
    # can_pin_messages в канале — если их нет, публикация всё равно прошла,
    # только пин не сработал, поэтому не роняем публикацию из-за этого.
    if category != "intro":
        return
    try:
        await bot.pin_chat_message(config.channel_id, sent.message_id)
    except Exception:
        logger.exception("Failed to pin intro post — публикация прошла, но закрепить не получилось (нет прав у бота?)")


async def _publish_generated_post(category: str | None = None) -> None:
    sync_docs_repo(config.finassist_docs_path)
    post = _generate_post_for_slot(category)
    sent = await _send_generated_post(config.channel_id, post)
    save_last_post_at(_POST_STATE_PATH, datetime.now(timezone.utc), title=_post_title(post))
    record_published_post(
        _HISTORY_PATH, category=category, summary=_post_history_summary(post), published_at=datetime.now(timezone.utc).isoformat()
    )
    await _pin_if_intro(category, sent)


@dp.message(Command("postnow"), F.func(_admin_filter))
async def cmd_postnow(message: Message, command: CommandObject) -> None:
    category = (command.args or "").strip().lower() or None
    if category and category not in _CATEGORY_LABELS:
        await message.answer(f"Неизвестная тема «{category}». Форматы: /postnow [poll|feature|personal|intro]")
        return
    await message.answer("Публикую…")
    try:
        await _publish_generated_post(category=category)
    except Exception:
        logger.exception("Manual post failed")
        await message.answer("⚠️ Не получилось опубликовать — смотри логи бота.")
        return
    await message.answer("✅ Опубликовано.")


@dp.message(Command("preview"), F.func(_admin_filter))
async def cmd_preview(message: Message, command: CommandObject) -> None:
    category = (command.args or "").strip().lower() or None
    if category and category not in _CATEGORY_LABELS:
        await message.answer(f"Неизвестная тема «{category}». Форматы: /preview [poll|feature|personal|intro]")
        return
    await message.answer("Генерирую предпросмотр (очередь/changelog не тронуты)…")
    try:
        sync_docs_repo(config.finassist_docs_path)
        preview_post = _generate_post_for_slot(category, dry_run=True)
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
    # (у intro и вовсе нет слота в _DAILY_SLOTS).
    category = (command.args or "").strip().lower() or None
    if category and category not in _CATEGORY_LABELS:
        await message.answer(f"Неизвестная тема «{category}». Форматы: /draft [poll|feature|personal|intro]")
        return
    if not config.admin_chat_id:
        await message.answer("Нужно задать CHANNEL_ADMIN_CHAT_ID, чтобы черновик было куда прислать.")
        return
    if _pending_draft is not None:
        await message.answer("Уже есть черновик, ждущий решения, — реши его сначала (кнопки выше).")
        return
    await message.answer("Генерирую черновик…")
    try:
        await _request_approval(category=category)
    except Exception:
        logger.exception("Manual draft request failed")
        await message.answer("⚠️ Не получилось сгенерировать черновик — смотри логи.")


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
            ]
        ]
    )


async def _request_approval(category: str | None = None) -> None:
    global _pending_draft, _pending_photo, _awaiting_feedback, _pending_slot_category
    sync_docs_repo(config.finassist_docs_path)
    post = _generate_post_for_slot(category)
    _pending_draft = post
    _pending_photo = None
    _awaiting_feedback = False
    _pending_slot_category = category
    category_hint = f" ({_CATEGORY_LABELS[category]})" if category else ""
    photo_hint = "\n\n📎 Пришли мне фото, если хочешь прикрепить его к посту." if post.kind == "text" else ""
    await bot.send_message(
        config.admin_chat_id,
        f"👀 <b>Черновик поста{category_hint} — нужно решение:</b>\n\n{_post_preview_text(post)}{photo_hint}",
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
    global _pending_draft, _pending_photo, _pending_slot_category
    if not _is_admin_callback(callback):
        await callback.answer("Только из админ-чата")
        return
    if _pending_draft is None:
        await callback.answer("Черновика уже нет — возможно, кто-то уже решил.")
        return
    post = _pending_draft
    photo = _pending_photo
    category = _pending_slot_category
    _pending_draft = None
    _pending_photo = None
    _pending_slot_category = None
    try:
        sent = await _send_generated_post(config.channel_id, post, photo=photo)
        save_last_post_at(_POST_STATE_PATH, datetime.now(timezone.utc), title=_post_title(post))
        record_published_post(
            _HISTORY_PATH, category=category, summary=_post_history_summary(post), published_at=datetime.now(timezone.utc).isoformat()
        )
        await _pin_if_intro(category, sent)
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
    # Реролл сохраняет тему СЛОТА (см. _pending_slot_category) — если это был
    # вечерний "personal"-слот, новый вариант тоже должен быть личной
    # историей (или fallback на фичу), а не случайным форматом.
    category = _pending_slot_category
    _pending_draft = None
    _pending_photo = None
    _awaiting_feedback = False
    if callback.message:
        await callback.message.edit_text("🔄 Черновик пропущен — готовлю другой вариант…")
    await callback.answer("Готовлю новый вариант")
    try:
        await _request_approval(category=category)
    except Exception:
        logger.exception("Failed to generate replacement draft after reroll")
        await bot.send_message(
            config.admin_chat_id, "⚠️ Не получилось сгенерировать новый вариант — попробуй /postnow позже или проверь логи."
        )


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
    global _pending_draft, _awaiting_feedback
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
        revised = revise_post(llm, _pending_draft, feedback, feedback_path=_FEEDBACK_PATH)
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
        # R-CONVENIENCE: расписание чисто по времени суток (_DAILY_SLOTS), не
        # "N часов после последнего поста" — если бот перезапустили и слот
        # уже прошёл, он просто пропускается (не постим задним числом), тот
        # же принцип, что раньше давал избежать спама после рестарта.
        now = datetime.now(timezone.utc)
        slot_dt, category = _next_slot_datetime(now)
        wait_seconds = (slot_dt - now).total_seconds()
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
    BotCommand(command="draft", description="Запросить черновик на ревью (можно: poll/feature/personal/intro)"),
    BotCommand(command="preview", description="Предпросмотр (можно: poll/feature/personal/intro)"),
    BotCommand(command="postnow", description="Опубликовать сейчас (можно: poll/feature/personal/intro)"),
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
