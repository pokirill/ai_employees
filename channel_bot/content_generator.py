from __future__ import annotations

from channel_bot.changelog_entries import mark_title_used, next_unused_entry
from channel_bot.content_queue import peek_next_topic, pop_next_topic
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


# R-COST: LLMConfig.reasoning_effort="minimal" убирает налог на скрытые
# reasoning-токены — без него 400 не хватало (см. память проекта/коммит
# про пустые ответы), с "minimal" хватает даже меньшего бюджета с запасом.
_POST_MAX_TOKENS = 500


def _write_post(llm: LLMClient, topic: str) -> str:
    return llm.chat(
        [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": f"Тема поста:\n{topic}"},
        ],
        max_tokens=_POST_MAX_TOKENS,
    )


def generate_next_post(
    llm: LLMClient,
    *,
    queue_path: str,
    changelog_path: str,
    used_state_path: str,
    docs_path: str,
    dry_run: bool = False,
) -> str:
    """dry_run=True — для /preview: генерирует текст, НЕ трогая состояние
    (не выкидывает тему из очереди, не помечает запись changelog
    использованной), чтобы предпросмотр не "тратил" реальный контент."""
    topic = peek_next_topic(queue_path) if dry_run else pop_next_topic(queue_path)
    if topic:
        return _write_post(llm, topic)

    entry = next_unused_entry(changelog_path, used_state_path)
    if entry:
        if not dry_run:
            mark_title_used(used_state_path, entry["title"])
        topic = f"{entry['title']}\n\n{entry['body']}"
        return _write_post(llm, topic)

    # R-COST: очередь и changelog исчерпаны — раньше тут было 2 вызова LLM
    # (сначала "придумай тему", потом отдельно "напиши пост по теме"), хотя
    # это один и тот же контекст проекта дважды в токенах. Одним вызовом —
    # просим модель самой выбрать тему и сразу написать пост, тем же
    # системным промптом, что и обычная генерация по теме.
    context = load_project_context(docs_path, max_chars=6000)
    return llm.chat(
        [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Очередь тем и записи AI_CHANGELOG.md закончились — выбери сама "
                    "интересную тему по контексту проекта ниже и сразу напиши пост "
                    f"по ней (без промежуточного описания темы).\n\nКонтекст проекта:\n{context}"
                ),
            },
        ],
        max_tokens=_POST_MAX_TOKENS,
    )
