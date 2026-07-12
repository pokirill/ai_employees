from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from channel_bot.content_generator import generate_next_post
from channel_bot.content_queue import append_topic
from shared.config import ChannelBotConfig, LLMConfig
from shared.docs_context import load_project_context, sync_docs_repo
from shared.llm_client import LLMClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("channel_bot")

config = ChannelBotConfig()
llm = LLMClient(LLMConfig())

bot = Bot(token=config.telegram_token, parse_mode=ParseMode.HTML)
dp = Dispatcher()

_QUEUE_PATH = "channel_bot/topics_queue.json"
_CHANGELOG_STATE_PATH = "channel_bot/used_changelog_titles.json"

_REPLY_SYSTEM_PROMPT = (
    "Ты — Кубышка, бот приложения для личных финансов, отвечаешь в чате обсуждения "
    "своего Telegram-канала. Тон тёплый, с характером, коротко (1-3 предложения). "
    "Никогда не стыди пользователя. Если не знаешь ответа — честно скажи, что "
    "передашь команде, не выдумывай."
)


@dp.message(Command("addtopic"))
async def cmd_addtopic(message: Message, command: CommandObject) -> None:
    topic = (command.args or "").strip()
    if not topic:
        await message.answer("Формат: /addtopic тема для следующего поста")
        return
    append_topic(_QUEUE_PATH, topic)
    await message.answer(f"✅ Добавил в очередь тем: «{topic}»")


def _changelog_path() -> str:
    return f"{config.finassist_docs_path}/AI_CHANGELOG.md"


async def post_scheduled_content() -> None:
    while True:
        try:
            sync_docs_repo(config.finassist_docs_path)
            post_text = generate_next_post(
                llm,
                queue_path=_QUEUE_PATH,
                changelog_path=_changelog_path(),
                used_state_path=_CHANGELOG_STATE_PATH,
                docs_path=config.finassist_docs_path,
            )
            await bot.send_message(config.channel_id, post_text)
        except Exception:
            logger.exception("Failed to publish scheduled post")
        await asyncio.sleep(config.post_interval_hours * 3600)


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
    await bot.send_chat_action(message.chat.id, "typing")
    context = load_project_context(config.finassist_docs_path, max_chars=4000)
    answer = llm.chat(
        [
            {"role": "system", "content": f"{_REPLY_SYSTEM_PROMPT}\n\nКонтекст проекта:\n{context}"},
            {"role": "user", "content": text},
        ],
        max_tokens=250,
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


async def main() -> None:
    _register_discussion_handler()
    asyncio.create_task(post_scheduled_content())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
