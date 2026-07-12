from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from shared.config import LLMConfig, TeamBotConfig
from shared.docs_context import load_project_context, sync_docs_repo
from shared.icloud_reminders import ICloudReminders, RemindersListNotFound, TaskNotFound
from shared.llm_client import LLMClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("team_bot")

config = TeamBotConfig()
llm = LLMClient(LLMConfig())
reminders = ICloudReminders(
    apple_id=config.icloud_apple_id,
    app_specific_password=config.icloud_app_password,
    list_name=config.icloud_reminders_list_name,
)

bot = Bot(token=config.telegram_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

_SYSTEM_PROMPT = (
    "Ты — ассистент команды разработки приложения «Кубышка» (FinAssist). "
    "Отвечай кратко и по делу, на русском. Если вопрос касается архитектуры, "
    "бэклога или истории решений проекта — опирайся на контекст ниже. Если "
    "контекста не хватает — честно скажи, что не уверен, не выдумывай детали."
)

# Короткая память диалога на чат — только для реплаев-продолжений /ask,
# не персистится (перезапуск бота = чистый лист). 3 последних обмена
# достаточно для уточняющих вопросов, не разрастаясь в полноценную БД.
_MAX_HISTORY_MESSAGES = 6
_conversation_history: dict[int, deque[dict[str, str]]] = defaultdict(lambda: deque(maxlen=_MAX_HISTORY_MESSAGES))

# Последний показанный /tasks список на чат — чтобы /done <номер> не заставлял
# перечитывать CalDAV и не требовал вводить длинный uid руками.
_last_shown_tasks: dict[int, list] = {}


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
        "/ask &lt;вопрос&gt; — спросить про проект (контекст из Docs/)\n"
        "Просто ответь (reply) на мой ответ — продолжу разговор с учётом контекста\n\n"
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


def _ask_llm(chat_id: int, question: str) -> str:
    sync_docs_repo(config.finassist_docs_path)
    context = load_project_context(config.finassist_docs_path)
    history = list(_conversation_history[chat_id])
    messages = [{"role": "system", "content": f"{_SYSTEM_PROMPT}\n\nКонтекст проекта:\n{context}"}]
    messages.extend(history)
    messages.append({"role": "user", "content": question})

    answer = llm.chat(messages)

    _conversation_history[chat_id].append({"role": "user", "content": question})
    _conversation_history[chat_id].append({"role": "assistant", "content": answer})
    return answer


@dp.message(Command("ask"))
async def cmd_ask(message: Message, command: CommandObject) -> None:
    question = (command.args or "").strip()
    if not question:
        await message.answer("Формат: /ask почему подушка блокирует цели")
        return
    await bot.send_chat_action(message.chat.id, "typing")
    answer = _ask_llm(message.chat.id, question)
    await message.answer(answer or "Не получилось сформулировать ответ.")


@dp.message(F.reply_to_message.func(lambda m: m is not None) & F.text & ~F.text.startswith("/"))
async def continue_conversation(message: Message) -> None:
    # Продолжение диалога с ассистентом: юзер отвечает на СВОЁ же сообщение
    # с /ask или на ответ бота — не нужно каждый раз перепечатывать /ask.
    if not message.reply_to_message or message.reply_to_message.from_user is None:
        return
    if message.reply_to_message.from_user.id != bot.id:
        return
    if not message.text:
        return
    await bot.send_chat_action(message.chat.id, "typing")
    answer = _ask_llm(message.chat.id, message.text.strip())
    await message.answer(answer or "Не получилось сформулировать ответ.")


async def main() -> None:
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
