from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from shared.config import LLMConfig, TeamBotConfig
from shared.docs_context import load_project_context, sync_docs_repo
from shared.icloud_reminders import ICloudReminders, RemindersListNotFound
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

bot = Bot(token=config.telegram_token, parse_mode=ParseMode.HTML)
dp = Dispatcher()

_SYSTEM_PROMPT = (
    "Ты — ассистент команды разработки приложения «Кубышка» (FinAssist). "
    "Отвечай кратко и по делу, на русском. Если вопрос касается архитектуры, "
    "бэклога или истории решений проекта — опирайся на контекст ниже. Если "
    "контекста не хватает — честно скажи, что не уверен, не выдумывай детали."
)


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "Команды:\n"
        "/task <текст> — записать задачу в общий список Напоминаний\n"
        "/ask <вопрос> — спросить про проект Кубышка (контекст из Docs/)"
    )


@dp.message(Command("task"))
async def cmd_task(message: Message, command: CommandObject) -> None:
    title = (command.args or "").strip()
    if not title:
        await message.answer("Формат: /task купить домен для канала")
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


@dp.message(Command("ask"))
async def cmd_ask(message: Message, command: CommandObject) -> None:
    question = (command.args or "").strip()
    if not question:
        await message.answer("Формат: /ask почему подушка блокирует цели")
        return
    await bot.send_chat_action(message.chat.id, "typing")
    sync_docs_repo(config.finassist_docs_path)
    context = load_project_context(config.finassist_docs_path)
    answer = llm.chat(
        [
            {"role": "system", "content": f"{_SYSTEM_PROMPT}\n\nКонтекст проекта:\n{context}"},
            {"role": "user", "content": question},
        ]
    )
    await message.answer(answer or "Не получилось сформулировать ответ.")


async def main() -> None:
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
