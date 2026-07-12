from __future__ import annotations

from channel_bot.changelog_entries import mark_title_used, next_unused_entry
from channel_bot.content_queue import pop_next_topic
from shared.docs_context import load_project_context
from shared.llm_client import LLMClient

_SYSTEM_PROMPT = (
    "Ты ведёшь Telegram-канал приложения «Кубышка» (личные финансы, копилка на цели). "
    "Пиши тёплые, живые посты на русском в стиле Duolingo — с характером, без "
    "канцелярита и без осуждения читателя. Формат: 1) короткий цепляющий заголовок "
    "(эмодзи уместны), 2) 2-4 предложения по существу. Если тема — техническое "
    "обновление, переведи её в понятный пользователю юзкейс («теперь можно X» вместо "
    "названия фичи/тикета). Не выдумывай цифры и факты, которых нет в теме."
)


def _write_post(llm: LLMClient, topic: str) -> str:
    return llm.chat(
        [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": f"Тема поста:\n{topic}"},
        ],
        max_tokens=400,
    )


def generate_next_post(
    llm: LLMClient,
    *,
    queue_path: str,
    changelog_path: str,
    used_state_path: str,
    docs_path: str,
) -> str:
    topic = pop_next_topic(queue_path)
    if topic:
        return _write_post(llm, topic)

    entry = next_unused_entry(changelog_path, used_state_path)
    if entry:
        mark_title_used(used_state_path, entry["title"])
        topic = f"{entry['title']}\n\n{entry['body']}"
        return _write_post(llm, topic)

    # Очередь и changelog исчерпаны — просим модель придумать тему самой,
    # опираясь на общий контекст проекта (бэклог/стратегию).
    context = load_project_context(docs_path, max_chars=6000)
    idea = llm.chat(
        [
            {
                "role": "system",
                "content": (
                    "Ты придумываешь тему для поста в Telegram-канале приложения «Кубышка» "
                    "(личные финансы). Основывайся на контексте проекта ниже. Верни ТОЛЬКО "
                    "краткое описание темы (1-2 предложения), не сам пост."
                ),
            },
            {"role": "user", "content": context},
        ],
        max_tokens=150,
    )
    return _write_post(llm, idea)
