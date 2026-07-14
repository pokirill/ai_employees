from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import BotCommand, BotCommandScopeChat, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from channel_bot.content_generator import generate_next_post
from channel_bot.content_queue import append_topic, load_queue, save_queue
from channel_bot.post_state import load_last_post_info, save_last_post_at, seconds_until_next_post
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
_CHANGELOG_STATE_PATH = "channel_bot/used_changelog_titles.json"
_POST_STATE_PATH = "channel_bot/last_post_state.json"

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
_pending_draft: str | None = None
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


@dp.message(Command("status"), F.func(_admin_filter))
async def cmd_status(message: Message) -> None:
    topics_count = len(load_queue(_QUEUE_PATH))
    remaining = seconds_until_next_post(_POST_STATE_PATH, config.post_interval_hours)
    hours_left = round(remaining / 3600, 1)
    last_info = load_last_post_info(_POST_STATE_PATH)
    if last_info:
        ago_hours = round((datetime.now(timezone.utc) - last_info["last_post_at"]).total_seconds() / 3600, 1)
        title = last_info["last_post_title"] or "(без заголовка)"
        last_line = f"📝 Последний пост: {ago_hours} ч назад — «{title}»"
    else:
        last_line = "📝 Постов ещё не было"
    if _effective_require_approval:
        mode_line = "📝 Режим: черновик на ревью" + (" — сейчас есть черновик, ждёт решения" if _pending_draft else "")
    else:
        mode_line = "🤖 Режим: полная автономность"
    await message.answer(
        f"{'⏸ Автопостинг на паузе (/resume)' if _posting_paused else '▶️ Автопостинг активен'}\n"
        f"{mode_line}\n"
        f"{last_line}\n"
        f"📋 Тем в очереди: {topics_count}\n"
        f"⏰ Интервал постинга: {config.post_interval_hours} ч\n"
        f"⏳ До следующего автопоста: ~{hours_left} ч\n"
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


async def _publish_generated_post() -> None:
    sync_docs_repo(config.finassist_docs_path)
    post_text = generate_next_post(
        llm,
        queue_path=_QUEUE_PATH,
        changelog_path=f"{config.finassist_docs_path}/AI_CHANGELOG.md",
        used_state_path=_CHANGELOG_STATE_PATH,
        docs_path=config.finassist_docs_path,
        beta_invite_url=config.beta_invite_url,
    )
    await bot.send_message(config.channel_id, post_text)
    save_last_post_at(_POST_STATE_PATH, datetime.now(timezone.utc), title=_extract_title(post_text))


@dp.message(Command("postnow"), F.func(_admin_filter))
async def cmd_postnow(message: Message) -> None:
    await message.answer("Публикую…")
    try:
        await _publish_generated_post()
    except Exception:
        logger.exception("Manual post failed")
        await message.answer("⚠️ Не получилось опубликовать — смотри логи бота.")
        return
    await message.answer("✅ Опубликовано.")


@dp.message(Command("preview"), F.func(_admin_filter))
async def cmd_preview(message: Message) -> None:
    await message.answer("Генерирую предпросмотр (очередь/changelog не тронуты)…")
    try:
        sync_docs_repo(config.finassist_docs_path)
        preview_text = generate_next_post(
            llm,
            queue_path=_QUEUE_PATH,
            changelog_path=f"{config.finassist_docs_path}/AI_CHANGELOG.md",
            used_state_path=_CHANGELOG_STATE_PATH,
            docs_path=config.finassist_docs_path,
            dry_run=True,
            beta_invite_url=config.beta_invite_url,
        )
    except Exception:
        logger.exception("Preview generation failed")
        await message.answer("⚠️ Не получилось сгенерировать предпросмотр.")
        return
    await message.answer(f"👀 <b>Предпросмотр следующего поста:</b>\n\n{preview_text}")


def _approval_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Опубликовать", callback_data="approve_post"),
                InlineKeyboardButton(text="🗑 Пропустить", callback_data="reject_post"),
            ]
        ]
    )


async def _request_approval() -> None:
    global _pending_draft
    sync_docs_repo(config.finassist_docs_path)
    post_text = generate_next_post(
        llm,
        queue_path=_QUEUE_PATH,
        changelog_path=f"{config.finassist_docs_path}/AI_CHANGELOG.md",
        used_state_path=_CHANGELOG_STATE_PATH,
        docs_path=config.finassist_docs_path,
        beta_invite_url=config.beta_invite_url,
    )
    _pending_draft = post_text
    await bot.send_message(
        config.admin_chat_id, f"👀 <b>Черновик поста — нужно решение:</b>\n\n{post_text}", reply_markup=_approval_keyboard()
    )


def _is_admin_callback(callback: CallbackQuery) -> bool:
    return bool(callback.message) and str(callback.message.chat.id) == config.admin_chat_id


@dp.callback_query(F.data == "approve_post")
async def cb_approve_post(callback: CallbackQuery) -> None:
    global _pending_draft
    if not _is_admin_callback(callback):
        await callback.answer("Только из админ-чата")
        return
    if _pending_draft is None:
        await callback.answer("Черновика уже нет — возможно, кто-то уже решил.")
        return
    text = _pending_draft
    _pending_draft = None
    try:
        await bot.send_message(config.channel_id, text)
        save_last_post_at(_POST_STATE_PATH, datetime.now(timezone.utc), title=_extract_title(text))
    except Exception:
        logger.exception("Failed to publish approved draft")
        await callback.answer("⚠️ Не получилось опубликовать — смотри логи.", show_alert=True)
        return
    if callback.message:
        await callback.message.edit_text(f"✅ Опубликовано:\n\n{text}")
    await callback.answer("Опубликовано")


@dp.callback_query(F.data == "reject_post")
async def cb_reject_post(callback: CallbackQuery) -> None:
    global _pending_draft
    if not _is_admin_callback(callback):
        await callback.answer("Только из админ-чата")
        return
    _pending_draft = None
    if callback.message:
        await callback.message.edit_text("🗑 Черновик пропущен — следующий будет по расписанию.")
    await callback.answer("Пропущено")


async def post_scheduled_content() -> None:
    while True:
        if _effective_require_approval and _pending_draft is not None:
            # Уже ждём решения по текущему черновику — не начинаем новый цикл
            # генерации поверх него.
            await asyncio.sleep(_PENDING_DRAFT_RECHECK_SECONDS)
            continue
        # R-CONVENIENCE: если бот перезапустили вскоре после предыдущего
        # поста (деплой/краш), не постим сразу повторно — ждём остаток
        # интервала. Без этого частые рестарты выглядели бы как спам в канале.
        wait_seconds = seconds_until_next_post(_POST_STATE_PATH, config.post_interval_hours)
        if wait_seconds > 0:
            await asyncio.sleep(wait_seconds)
            continue
        if _posting_paused:
            await asyncio.sleep(60)
            continue
        try:
            if _effective_require_approval:
                await _request_approval()
                # last_post_at обновится при апруве (cb_approve_post) — тут
                # не спим долго, следующая итерация быстро увидит pending и
                # уйдёт в ветку выше.
            else:
                await _publish_generated_post()
                await asyncio.sleep(config.post_interval_hours * 3600)
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
    BotCommand(command="preview", description="Предпросмотр следующего поста"),
    BotCommand(command="postnow", description="Опубликовать сейчас"),
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
