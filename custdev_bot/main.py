from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Message

from custdev_bot.triggers import mentions_money_tracking
from shared.config import CustdevBotConfig, LLMConfig
from shared.llm_client import LLMClient
from shared.rate_limiter import SlidingWindowLimiter

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("custdev_bot")

config = CustdevBotConfig()
llm = LLMClient(LLMConfig())

bot = Bot(token=config.telegram_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# R-COST: не более N реплик в час НА ЧАТ — реактивность по ключевым словам
# уже сильно ограничивает частоту, лимит — страховка от чрезмерного участия
# в одном особо разговорчивом про деньги чате.
_reply_limiter = SlidingWindowLimiter(max_calls=config.max_replies_per_hour, window_seconds=3600)

# Раскрытие: бот сразу называет себя от команды «Кубышка» — это открытый,
# а не анонимный сбор мнений (в отличие от изначально обсуждавшегося
# скрытого варианта, который сознательно не стали делать).
_OPENING_SYSTEM_PROMPT = (
    "Ты бот команды приложения «Кубышка» (личные финансы). Ты ОБЯЗАТЕЛЬНО "
    "представляешься в первом же сообщении ('Привет! Я бот команды "
    "приложения «Кубышка»'), затем по-дружески спрашиваешь, КАК именно "
    "человек следит за своими тратами (приложение/таблица/на глаз/никак) и "
    "ПОЧЕМУ он выбрал такой способ. ТОЛЬКО представление + один вопрос, "
    "суммарно не больше 2 предложений. НЕ предлагай решения, планы, советы "
    "или помощь — просто искренне интересуешься, без осуждения."
)

# После того как человек ответил — благодарим и явно раскрываем цель: это
# исследование для приложения, ссылка на App Store, данные идут на
# улучшение сервиса. Это и есть раскрытие цели опроса, а не только личности.
def _closing_message() -> str:
    link_line = (
        f"\n\n📲 Кубышка в App Store: {config.app_store_url}"
        if config.app_store_url
        else "\n\n📲 Кубышка скоро выйдет в App Store."
    )
    return (
        "Спасибо, что поделились! 🙏 Кубышка — это приложение для учёта трат "
        "и накоплений, и то, что вы рассказали, помогает нам делать сервис "
        "лучше." + link_line
    )


# R-COST: короткий классификатор (max_tokens=10, temperature=0) — дешёвая
# проверка "стоит ли показать команде", не полноценная генерация.
_INSIGHT_JUDGE_PROMPT = (
    "Оцени сообщение пользователя из чата — рассказывает ли он что-то "
    "содержательное о том, как/почему следит (или не следит) за личными "
    "финансами (конкретный способ, привычка, проблема, эмоция). Ответь "
    "ТОЛЬКО 'да' или 'нет', без пояснений."
)


def _is_reply_to_bot(message: Message) -> bool:
    reply = message.reply_to_message
    return bool(reply and reply.from_user and reply.from_user.id == bot.id)


async def _forward_insight_if_interesting(message: Message) -> None:
    if not config.team_chat_id:
        return
    text = message.text or ""
    if not text:
        return
    verdict = llm.chat(
        [
            {"role": "system", "content": _INSIGHT_JUDGE_PROMPT},
            {"role": "user", "content": text},
        ],
        max_tokens=10,
        temperature=0.0,
    )
    if "да" not in verdict.lower():
        return
    chat_title = message.chat.title or str(message.chat.id)
    author = message.from_user.full_name if message.from_user else "неизвестно"
    try:
        await bot.send_message(
            config.team_chat_id,
            f"💡 <b>Кастдев-инсайт</b> (чат «{chat_title}», {author}):\n\n{text}",
        )
    except Exception:
        logger.exception("Failed to forward custdev insight to team chat")


@dp.message(F.text)
async def handle_message(message: Message) -> None:
    if message.from_user and message.from_user.is_bot:
        # Защита от зацикливания — та же логика, что в team_bot/channel_bot:
        # два бота, отвечающие друг другу, могли бы уйти в бесконечный цикл.
        return

    text = message.text or ""

    if _is_reply_to_bot(message):
        if not _reply_limiter.allow(message.chat.id):
            return
        await message.reply(_closing_message())
        await _forward_insight_if_interesting(message)
        return

    if not mentions_money_tracking(text):
        return
    if not _reply_limiter.allow(message.chat.id):
        return
    await bot.send_chat_action(message.chat.id, "typing")
    answer = llm.chat(
        [
            {"role": "system", "content": _OPENING_SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        max_tokens=150,
    )
    if answer:
        await message.reply(answer)


async def main() -> None:
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
