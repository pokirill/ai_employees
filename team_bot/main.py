from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict, deque

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from shared.config import LLMConfig, TeamBotConfig
from shared.context_heuristic import question_needs_project_context
from shared.docs_context import load_project_context, sync_docs_repos
from shared.icloud_reminders import ICloudReminders, RemindersListNotFound, TaskNotFound
from shared.llm_client import LLMClient
from shared.rate_limiter import SlidingWindowLimiter

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("team_bot")

config = TeamBotConfig()
llm = LLMClient(LLMConfig(), model_override=config.model_override)
reminders = ICloudReminders(
    apple_id=config.icloud_apple_id,
    app_specific_password=config.icloud_app_password,
    list_name=config.icloud_reminders_list_name,
)

# R-COST: см. shared/rate_limiter.py — не более N вопросов ассистенту в час
# на чат, чтобы один болтливый чат не сжёг весь бюджет OpenRouter.
_rate_limiter = SlidingWindowLimiter(max_calls=config.max_questions_per_hour, window_seconds=3600)

# R-COST: контекст проекта (Docs/*.md обоих репо) — не на каждый вопрос, а
# только когда похоже, что он реально нужен (см. question_needs_project_context).
# Меньше max_chars, чем полный дефолт docs_context — команд-бот не обязан
# видеть ВСЁ, только достаточно для конкретного вопроса.
_CONTEXT_MAX_CHARS = 6000
_ANSWER_MAX_TOKENS = 400

bot = Bot(token=config.telegram_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

_SYSTEM_PROMPT = (
    "Ты — ассистент команды разработки приложения «Кубышка» (iOS-репозиторий "
    "FinAssist + бэкенд Finik-backend). Отвечай кратко и по делу, на русском. "
    "Если вопрос касается архитектуры, бэклога или истории решений проекта — "
    "опирайся на контекст ниже (там пометки [FinAssist]/[Finik-backend], "
    "откуда какой факт). Если контекста не хватает — честно скажи, что не "
    "уверен, не выдумывай детали."
)

# Короткая память диалога на чат — только для реплаев-продолжений /ask,
# не персистится (перезапуск бота = чистый лист). 3 последних обмена
# достаточно для уточняющих вопросов, не разрастаясь в полноценную БД.
_MAX_HISTORY_MESSAGES = 6
_conversation_history: dict[int, deque[dict[str, str]]] = defaultdict(lambda: deque(maxlen=_MAX_HISTORY_MESSAGES))

# Последний показанный /tasks список на чат — чтобы /done <номер> не заставлял
# перечитывать CalDAV и не требовал вводить длинный uid руками.
_last_shown_tasks: dict[int, list] = {}

# Заполняется в main() через bot.get_me() перед стартом polling — нужен, чтобы
# распознавать «@ИмяБота вопрос» в групповом чате как обращение к ассистенту.
_bot_username: str | None = None


@dp.message(Command("start"))
@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "Привет! Я помощник команды по проекту «Кубышка».\n\n"
        "<b>Задачи</b>\n"
        "/task &lt;текст&gt; — записать задачу в общий список Напоминаний\n"
        "(можно ответить командой /task на чьё-то сообщение — возьму текст оттуда)\n"
        "/tasks — показать незавершённые задачи списка\n"
        "/done &lt;номер&gt; — отметить задачу из /tasks выполненной\n\n"
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
    if not title and message.reply_to_message and message.reply_to_message.text:
        title = message.reply_to_message.text.strip()
    if not title:
        await message.answer(
            "Формат: /task купить домен для канала\n"
            "Или ответь командой /task на сообщение с текстом задачи."
        )
        return
    author = message.from_user.full_name if message.from_user else "неизвестно"
    try:
        reminders.add_task(title, notes=f"От {author} в Telegram")
    except RemindersListNotFound as exc:
        await message.answer(f"⚠️ {exc}")
        return
    except Exception:
        logger.exception("Failed to add reminder")
        await message.answer("⚠️ Не получилось записать задачу — попробуй ещё раз чуть позже.")
        return
    await message.answer(f"✅ Записал в Напоминания: «{title}»")


@dp.message(Command("tasks"))
async def cmd_tasks(message: Message) -> None:
    try:
        tasks = reminders.list_open_tasks()
    except RemindersListNotFound as exc:
        await message.answer(f"⚠️ {exc}")
        return
    except Exception:
        logger.exception("Failed to list reminders")
        await message.answer("⚠️ Не получилось прочитать список задач.")
        return

    if not tasks:
        await message.answer("Незавершённых задач нет 🎉")
        _last_shown_tasks[message.chat.id] = []
        return

    _last_shown_tasks[message.chat.id] = tasks
    lines = [f"{i}. {task.title}" for i, task in enumerate(tasks, start=1)]
    await message.answer(
        "Незавершённые задачи:\n" + "\n".join(lines) + "\n\nОтметить выполненной: /done <номер>"
    )


@dp.message(Command("done"))
async def cmd_done(message: Message, command: CommandObject) -> None:
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Формат: /done 2 (номер из последнего /tasks)")
        return

    index = int(arg) - 1
    cached = _last_shown_tasks.get(message.chat.id)
    if not cached or not (0 <= index < len(cached)):
        await message.answer("Сначала вызови /tasks, чтобы увидеть актуальные номера.")
        return

    task = cached[index]
    try:
        title = reminders.complete_task(task.uid)
    except TaskNotFound:
        await message.answer("⚠️ Задача уже выполнена или удалена — обнови /tasks.")
        return
    except Exception:
        logger.exception("Failed to complete reminder")
        await message.answer("⚠️ Не получилось отметить задачу выполненной.")
        return

    await message.answer(f"✅ Готово: «{title}»")


def _ask_llm(chat_id: int, question: str, *, force_context: bool = False) -> str:
    # R-COST: контекст грузим, только если он реально нужен — иначе на
    # "привет"/"спасибо" улетал бы тот же объём токенов, что на серьёзный
    # архитектурный вопрос. /ask — явное намерение спросить, форсируем контекст.
    if force_context or question_needs_project_context(question):
        sync_docs_repos(config.docs_paths)
        context = load_project_context(config.docs_paths, max_chars=_CONTEXT_MAX_CHARS)
        system_content = f"{_SYSTEM_PROMPT}\n\nКонтекст проекта:\n{context}"
    else:
        system_content = _SYSTEM_PROMPT

    history = list(_conversation_history[chat_id])
    messages = [{"role": "system", "content": system_content}]
    messages.extend(history)
    messages.append({"role": "user", "content": question})

    answer = llm.chat(messages, max_tokens=_ANSWER_MAX_TOKENS)

    _conversation_history[chat_id].append({"role": "user", "content": question})
    _conversation_history[chat_id].append({"role": "assistant", "content": answer})
    return answer


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
    answer = _ask_llm(message.chat.id, question, force_context=True)
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
    answer = _ask_llm(message.chat.id, question)
    await message.answer(answer or "Не получилось сформулировать ответ.")


async def main() -> None:
    me = await bot.get_me()
    global _bot_username
    _bot_username = me.username
    logger.info("Bot username resolved: @%s", _bot_username)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
