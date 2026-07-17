from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict, deque
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo

from shared.config import LLMConfig, TaskBoardConfig, TeamBotConfig
from shared.context_heuristic import question_needs_project_context
from shared.docs_context import load_project_context, sync_docs_repos, topic_context_files
from shared.icloud_reminders import ICloudReminders
from shared.icloud_reminders import TaskNotFound as ReminderTaskNotFound
from shared.llm_client import LLMClient
from shared.rate_limiter import SlidingWindowLimiter
from shared.reminder_digest import build_reminder_digest
from shared.task_store import TaskNotFound, TaskStore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("team_bot")

config = TeamBotConfig()
board_config = TaskBoardConfig()
llm = LLMClient(LLMConfig(), model_override=config.model_override)
reminders = ICloudReminders(
    apple_id=config.icloud_apple_id,
    app_specific_password=config.icloud_app_password,
    list_name=config.icloud_reminders_list_name,
)
# Доска задач (мини-апп) — источник правды для claim/статус/комментариев,
# Напоминания остаются best-effort зеркалом (см. cmd_task/cmd_done).
tasks_store = TaskStore(board_config.db_path)

# R-COST: см. shared/rate_limiter.py — не более N вопросов ассистенту в час
# на чат, чтобы один болтливый чат не сжёг весь бюджет LLM.
_rate_limiter = SlidingWindowLimiter(max_calls=config.max_questions_per_hour, window_seconds=3600)

# R-COST: контекст проекта (Docs/*.md обоих репо) — не на каждый вопрос, а
# только когда похоже, что он реально нужен (см. question_needs_project_context).
# Меньше max_chars, чем полный дефолт docs_context — команд-бот не обязан
# видеть ВСЁ, только достаточно для конкретного вопроса. Поднят с 6000 до
# 9000: с добавлением тематических файлов (topic_context_files) есть что
# реально положить в бюджет — раньше бот мог отвечать только по BACKLOG/
# CHANGELOG/ARCHITECTURE, теперь по вопросу подключаются файлы вроде
# BUSINESS_LOGIC.md/ONBOARDING_FLOW.md/GOALS_SCREEN.md и т.п.
_CONTEXT_MAX_CHARS = 9000
# R-COST: LLMConfig.reasoning_effort="minimal" (см. shared/llm_client.py)
# убирает налог на скрытые reasoning-токены reasoning-моделей — без него
# 400 не хватало (модель гасила весь бюджет на "раздумья" и возвращала
# пустую строку), с "minimal" даже меньшего бюджета хватает с запасом на
# полноценный ответ (проверено реальным вызовом API).
_ANSWER_MAX_TOKENS = 500

bot = Bot(token=config.telegram_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

_SYSTEM_PROMPT = (
    "Ты — ассистент команды разработки приложения «Кубышка» (iOS-репозиторий "
    "FinAssist + бэкенд Finik-backend). Отвечай кратко и по делу, на русском. "
    "Если вопрос касается архитектуры, бэклога или истории решений проекта — "
    "опирайся на контекст ниже (там пометки [FinAssist]/[Finik-backend], "
    "откуда какой факт). Если контекста не хватает — честно скажи, что не "
    "уверен, не выдумывай детали.\n"
    "Пиши только по-русски: не используй английские, испанские или другие "
    "иноязычные слова и аббревиатуры (например, недопустимо 'BBDD' вместо "
    "'БД') — используй русские термины, принятые в команде (БД, бэкенд, "
    "API можно оставить как есть — это устоявшиеся технические термины, а "
    "не случайные иноязычные вставки)."
)

# Короткая память диалога — только для реплаев-продолжений /ask, не
# персистится (перезапуск бота = чистый лист). 3 последних обмена достаточно
# для уточняющих вопросов, не разрастаясь в полноценную БД.
#
# Ключ — (chat_id, user_id), НЕ просто chat_id: в групповом чате несколько
# человек могут спрашивать ассистента одновременно, и если бы история была
# общей на чат, продолжение (reply/упоминание) одного человека подмешивало
# бы контекст чужого недавнего вопроса. Лимит вопросов в час (_rate_limiter)
# сознательно остаётся per-chat — это защита бюджета, а не память диалога,
# смешивать их не нужно.
_MAX_HISTORY_MESSAGES = 6
_HistoryKey = tuple[int, int]
_conversation_history: dict[_HistoryKey, deque[dict[str, str]]] = defaultdict(
    lambda: deque(maxlen=_MAX_HISTORY_MESSAGES)
)


def _history_key(message: Message) -> _HistoryKey:
    user_id = message.from_user.id if message.from_user else 0
    return (message.chat.id, user_id)

# Заполняется в main() через bot.get_me() перед стартом polling — нужен, чтобы
# распознавать «@ИмяБота вопрос» в групповом чате как обращение к ассистенту.
_bot_username: str | None = None


@dp.message(Command("start"))
@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "Привет! Я помощник команды по проекту «Кубышка».\n\n"
        "<b>Задачи</b>\n"
        "/task &lt;текст&gt; — записать задачу на общую доску\n"
        "(можно ответить командой /task на чьё-то сообщение — возьму текст оттуда)\n"
        "/tasks — показать незавершённые задачи\n"
        "/mytasks — задачи, которые взял именно я\n"
        "/claim &lt;номер&gt; — взять задачу себе\n"
        "/unclaim &lt;номер&gt; — отпустить задачу\n"
        "/testing &lt;номер&gt; — отправить на тестирование\n"
        "/done &lt;номер&gt; — отметить задачу выполненной\n"
        "/comment &lt;номер&gt; &lt;текст&gt; — комментарий к задаче\n"
        "/rename &lt;номер&gt; &lt;текст&gt; — переименовать задачу\n"
        "/board — открыть доску задач (мини-апп: карточка, комментарии, статус) — в личке\n"
        "/remindnow — прислать дайджест открытых задач сейчас (обычно раз в день сам)\n\n"
        "<b>Ассистент</b>\n"
        "/ask &lt;вопрос&gt; — спросить про проект (контекст из Docs/ обоих репо)\n"
        "В группе: упомяни меня (@бот вопрос) или ответь на моё сообщение\n"
        "В личке: пиши что угодно, отвечу как ассистент без команд\n\n"
        f"(до {config.max_questions_per_hour} вопросов ассистенту в час на чат — бережём бюджет)\n\n"
        "<b>Утилита</b>\n"
        "/id — показать ID этого чата (нужно для настройки)"
    )


@dp.message(Command("id"))
async def cmd_id(message: Message) -> None:
    await message.answer(f"Chat ID: <code>{message.chat.id}</code>")


@dp.message(Command("task"))
async def cmd_task(message: Message, command: CommandObject) -> None:
    title = (command.args or "").strip()
    if not title and message.reply_to_message:
        # .text для обычных сообщений, .caption — если отвечают на фото/видео
        # с подписью (например, скриншот бага с описанием в подписи).
        reply = message.reply_to_message
        title = (reply.text or reply.caption or "").strip()
    if not title:
        await message.answer(
            "Формат: /task купить домен для канала\n"
            "Или ответь командой /task на сообщение с текстом задачи."
        )
        return
    author = message.from_user.full_name if message.from_user else "неизвестно"
    task = tasks_store.add_task(title, created_by=author)
    try:
        uid = reminders.add_task(title, notes=f"От {author} в Telegram, доска #{task.id}")
        tasks_store.set_reminder_uid(task.id, uid)
    except Exception:
        # Доска — источник правды, Напоминания — best-effort зеркало для тех,
        # кто смотрит список на телефоне. Сбой зеркалирования (список ещё не
        # расшарен, неверный пароль и т.п.) не должен ронять саму запись задачи.
        logger.exception("Failed to mirror task to iCloud Reminders")
    await message.answer(f"✅ Задача #{task.id} записана: «{title}»\nОткрыть доску: /board")


@dp.message(Command("tasks"))
async def cmd_tasks(message: Message) -> None:
    tasks = tasks_store.list_tasks(include_done=False)
    if not tasks:
        await message.answer("Незавершённых задач нет 🎉")
        return

    lines = []
    for task in tasks:
        claim = f" (взял: {task.claimed_by})" if task.claimed_by else ""
        testing = " [тестируется]" if task.status == "testing" else ""
        lines.append(f"#{task.id} {task.title}{testing}{claim}")
    await message.answer(
        "Незавершённые задачи:\n" + "\n".join(lines) + "\n\nОтметить выполненной: /done <номер>, или /board"
    )


@dp.message(Command("done"))
async def cmd_done(message: Message, command: CommandObject) -> None:
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Формат: /done 7 (номер из /tasks или доски)")
        return

    try:
        task = tasks_store.complete_task(int(arg))
    except TaskNotFound:
        await message.answer("⚠️ Задача с таким номером не найдена — проверь /tasks.")
        return

    if task.reminder_uid:
        try:
            reminders.complete_task(task.reminder_uid)
        except ReminderTaskNotFound:
            pass
        except Exception:
            logger.exception("Failed to mirror completion to iCloud Reminders")

    await message.answer(f"✅ Готово: «{task.title}»")


def _requester_identity(message: Message) -> tuple[str, int | None]:
    if not message.from_user:
        return "неизвестно", None
    return message.from_user.full_name, message.from_user.id


@dp.message(Command("claim"))
async def cmd_claim(message: Message, command: CommandObject) -> None:
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Формат: /claim 7 (номер из /tasks или доски)")
        return
    name, user_id = _requester_identity(message)
    try:
        task = tasks_store.claim_task(int(arg), name, user_id)
    except TaskNotFound:
        await message.answer("⚠️ Задача с таким номером не найдена — проверь /tasks.")
        return
    await message.answer(f"🙋 Взял в работу: «{task.title}»")


@dp.message(Command("unclaim"))
async def cmd_unclaim(message: Message, command: CommandObject) -> None:
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Формат: /unclaim 7")
        return
    try:
        task = tasks_store.unclaim_task(int(arg))
    except TaskNotFound:
        await message.answer("⚠️ Задача с таким номером не найдена — проверь /tasks.")
        return
    await message.answer(f"🔓 Отпустил: «{task.title}»")


@dp.message(Command("testing"))
async def cmd_testing(message: Message, command: CommandObject) -> None:
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Формат: /testing 7")
        return
    try:
        task = tasks_store.mark_testing(int(arg))
    except TaskNotFound:
        await message.answer("⚠️ Задача с таким номером не найдена — проверь /tasks.")
        return
    await message.answer(f"🧪 На тестировании: «{task.title}»")


@dp.message(Command("mytasks"))
async def cmd_mytasks(message: Message) -> None:
    _, user_id = _requester_identity(message)
    mine = [t for t in tasks_store.list_tasks(include_done=False) if t.claimed_by_user_id == user_id]
    if not mine:
        await message.answer("У тебя сейчас нет задач в работе. Взять: /claim <номер> или /board")
        return
    lines = []
    for task in mine:
        testing = " [тестируется]" if task.status == "testing" else ""
        lines.append(f"#{task.id} {task.title}{testing}")
    await message.answer("Твои задачи в работе:\n" + "\n".join(lines))


@dp.message(Command("comment"))
async def cmd_comment(message: Message, command: CommandObject) -> None:
    args = (command.args or "").strip()
    parts = args.split(maxsplit=1)
    if len(parts) < 2 or not parts[0].isdigit():
        await message.answer("Формат: /comment 7 текст комментария")
        return
    name, _ = _requester_identity(message)
    try:
        task = tasks_store.add_comment(int(parts[0]), name, parts[1].strip())
    except TaskNotFound:
        await message.answer("⚠️ Задача с таким номером не найдена — проверь /tasks.")
        return
    await message.answer(f"💬 Комментарий добавлен к «{task.title}»")


@dp.message(Command("rename"))
async def cmd_rename(message: Message, command: CommandObject) -> None:
    args = (command.args or "").strip()
    parts = args.split(maxsplit=1)
    if len(parts) < 2 or not parts[0].isdigit():
        await message.answer("Формат: /rename 7 новое название задачи")
        return
    try:
        task = tasks_store.rename_task(int(parts[0]), parts[1].strip())
    except TaskNotFound:
        await message.answer("⚠️ Задача с таким номером не найдена — проверь /tasks.")
        return
    await message.answer(f"✏️ Переименовано: «{task.title}»")


@dp.message(Command("board"))
async def cmd_board(message: Message) -> None:
    if not board_config.webapp_url:
        await message.answer(
            "Мини-апп с доской задач ещё не задеплоен на публичный https-адрес "
            "(WEBAPP_URL в .env) — как будет, здесь появится кнопка."
        )
        return
    if message.chat.type != "private":
        # Telegram открывает web_app-кнопки из инлайн-клавиатуры только в
        # личном чате с ботом — в группе кнопка не сработает.
        await message.answer("Открой доску в личке со мной — напиши мне /board напрямую.")
        return
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📋 Открыть доску задач", web_app=WebAppInfo(url=board_config.webapp_url))]
        ]
    )
    await message.answer("Доска задач команды:", reply_markup=keyboard)


@dp.message(Command("remindnow"))
async def cmd_remind_now(message: Message) -> None:
    # Ручной триггер того же дайджеста, что шлёт reminder_loop раз в день —
    # чтобы проверить формат/содержание, не дожидаясь TEAM_REMINDER_HOUR.
    digest = build_reminder_digest(tasks_store.list_tasks(include_done=False))
    await message.answer(digest or "Открытых задач нет — напоминать не о чем 🎉")


def _seconds_until_next_reminder() -> float:
    now = datetime.now()
    target = now.replace(hour=config.reminder_hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def reminder_loop() -> None:
    # Раз в сутки, в config.reminder_hour по местному времени машины —
    # дайджест открытых задач в TEAM_CHAT_ID. Не персистится и не защищено от
    # пропуска при рестарте бота ровно в момент отправки — это ежедневное
    # напоминание, не критичная нотификация, простой sleep-цикл достаточен.
    while True:
        try:
            await asyncio.sleep(_seconds_until_next_reminder())
            digest = build_reminder_digest(tasks_store.list_tasks(include_done=False))
            if digest:
                await bot.send_message(config.team_chat_id, digest)
        except Exception:
            logger.exception("Reminder loop iteration failed")


def _ask_llm(history_key: _HistoryKey, question: str, *, force_context: bool = False) -> str:
    if not llm.config.api_key:
        # OPENAI_API_KEY не обязателен для старта бота (задачи/доска не
        # используют LLM), но без него ассистент отвечать не может — явная
        # ошибка пользователю лучше молчания.
        raise RuntimeError("Ассистент ещё не настроен: не задан OPENAI_API_KEY.")
    # R-COST: контекст грузим, только если он реально нужен — иначе на
    # "привет"/"спасибо" улетал бы тот же объём токенов, что на серьёзный
    # архитектурный вопрос. /ask — явное намерение спросить, форсируем контекст.
    if force_context or question_needs_project_context(question):
        sync_docs_repos(config.docs_paths)
        extra_files = topic_context_files(question)
        context = load_project_context(config.docs_paths, max_chars=_CONTEXT_MAX_CHARS, extra_filenames=extra_files)
        system_content = f"{_SYSTEM_PROMPT}\n\nКонтекст проекта:\n{context}"
    else:
        system_content = _SYSTEM_PROMPT

    history = list(_conversation_history[history_key])
    messages = [{"role": "system", "content": system_content}]
    messages.extend(history)
    messages.append({"role": "user", "content": question})

    answer = llm.chat(messages, max_tokens=_ANSWER_MAX_TOKENS)

    _conversation_history[history_key].append({"role": "user", "content": question})
    _conversation_history[history_key].append({"role": "assistant", "content": answer})
    return answer


def _assistant_error_message(exc: Exception) -> str:
    # Наши собственные сообщения (например, "не настроен OPENAI_API_KEY")
    # безопасно показать как есть. Остальное — сеть/провайдер/неожиданное —
    # логируем полностью, но в чат не тащим детали (могут содержать служебную
    # информацию о запросе), чтобы не палить внутренности бота в общем чате.
    logger.exception("Assistant call failed")
    if isinstance(exc, RuntimeError):
        return str(exc)
    return "Не получилось спросить ассистента — что-то не так на стороне LLM-провайдера. Попробуй ещё раз чуть позже."


def _reject_if_rate_limited(message: Message) -> bool:
    """True, если сообщение НАДО отклонить (лимит исчерпан) — уже отправляет
    юзеру объяснение сама, вызывающему коду останется просто return."""
    if _rate_limiter.allow(message.chat.id):
        return False
    return True


@dp.message(Command("ask"))
async def cmd_ask(message: Message, command: CommandObject) -> None:
    question = (command.args or "").strip()
    if not question:
        await message.answer("Формат: /ask почему подушка блокирует цели")
        return
    if _reject_if_rate_limited(message):
        wait_min = round(_rate_limiter.seconds_until_available(message.chat.id) / 60)
        await message.answer(f"⏳ Лимит вопросов на этот час исчерпан. Попробуй через ~{wait_min} мин.")
        return
    await bot.send_chat_action(message.chat.id, "typing")
    try:
        answer = _ask_llm(_history_key(message), question, force_context=True)
    except Exception as exc:
        await message.answer(f"⚠️ {_assistant_error_message(exc)}")
        return
    await message.answer(answer or "Не получилось сформулировать ответ.")


def _strip_mention(text: str) -> str:
    if not _bot_username:
        return text
    pattern = re.compile(re.escape(f"@{_bot_username}"), re.IGNORECASE)
    return pattern.sub("", text).strip()


def _is_reply_to_bot(message: Message) -> bool:
    reply = message.reply_to_message
    return bool(reply and reply.from_user and reply.from_user.id == bot.id)


def _mentions_bot(message: Message) -> bool:
    if not _bot_username or not message.text:
        return False
    return f"@{_bot_username.lower()}" in message.text.lower()


def _should_respond_as_assistant(message: Message) -> bool:
    if not message.text or message.text.startswith("/"):
        return False
    if message.from_user and message.from_user.is_bot:
        # Без этого два бота, отвечающие друг другу (наш ассистент + любой
        # другой бот в том же чате), могли бы уйти в бесконечный цикл
        # ответов — каждый круг это реальный оплаченный вызов LLM.
        return False
    # Личка — всегда диалог с ассистентом, обращение по имени избыточно.
    if message.chat.type == "private":
        return True
    # В группе — только если реально ОБРАТИЛИСЬ: ответили на сообщение бота
    # или упомянули его по имени. Отвечать на каждую реплику в общем чате
    # было бы шумно (та же логика, что и в channel_bot для чата обсуждения).
    return _is_reply_to_bot(message) or _mentions_bot(message)


@dp.message(F.func(_should_respond_as_assistant))
async def handle_assistant_message(message: Message) -> None:
    question = _strip_mention(message.text or "")
    if not question:
        await message.reply("Да? Спрашивай — я тут 🙂")
        return
    if _reject_if_rate_limited(message):
        wait_min = round(_rate_limiter.seconds_until_available(message.chat.id) / 60)
        await message.reply(f"⏳ Лимит вопросов на этот час исчерпан. Попробуй через ~{wait_min} мин.")
        return
    await bot.send_chat_action(message.chat.id, "typing")
    try:
        answer = _ask_llm(_history_key(message), question)
    except Exception as exc:
        await message.answer(f"⚠️ {_assistant_error_message(exc)}")
        return
    await message.answer(answer or "Не получилось сформулировать ответ.")


_BOT_COMMANDS = [
    BotCommand(command="task", description="Записать задачу на доску"),
    BotCommand(command="tasks", description="Незавершённые задачи"),
    BotCommand(command="mytasks", description="Задачи, которые взял я"),
    BotCommand(command="claim", description="Взять задачу себе"),
    BotCommand(command="unclaim", description="Отпустить задачу"),
    BotCommand(command="testing", description="Отправить задачу на тестирование"),
    BotCommand(command="done", description="Отметить задачу выполненной"),
    BotCommand(command="comment", description="Комментарий к задаче"),
    BotCommand(command="rename", description="Переименовать задачу"),
    BotCommand(command="board", description="Доска задач (мини-апп)"),
    BotCommand(command="remindnow", description="Дайджест открытых задач сейчас"),
    BotCommand(command="ask", description="Спросить ассистента про проект"),
    BotCommand(command="id", description="ID текущего чата"),
    BotCommand(command="help", description="Список команд"),
]



async def main() -> None:
    me = await bot.get_me()
    global _bot_username
    _bot_username = me.username
    logger.info("Bot username resolved: @%s", _bot_username)
    # Без этого Telegram не показывает автодополнение команд при вводе "/".
    await bot.set_my_commands(_BOT_COMMANDS)
    if config.team_chat_id:
        asyncio.create_task(reminder_loop())
    else:
        logger.info("TEAM_CHAT_ID не задан — ежедневный дайджест отключён (доступен вручную через /remindnow)")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
