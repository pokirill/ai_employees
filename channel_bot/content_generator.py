from __future__ import annotations

import random

from channel_bot.changelog_entries import mark_title_used, next_unused_entry
from channel_bot.content_queue import peek_next_topic, pop_next_topic
from shared.docs_context import load_project_context
from shared.llm_client import LLMClient

# Формат по итогам ревью с Кирилл: сначала "джоба" (житейская проблема
# читателя с деньгами) в юмористической форме, ПОТОМ раскрытие, что в
# приложении есть фича, которая её решает — не наоборот и не просто
# "юзкейс фичи" без сначала обозначенной проблемы.
#
# Дополнено по референсам каналов, которые скинул Кирилл (RationalAnswer —
# для глубины/юмора, не для ДЛИНЫ; более короткие личные каналы — для тона
# и приёма звать читателей необычным прозвищем) + прямому фидбеку "часто
# получается слишком много текста, надо короче и попроще".
_SYSTEM_PROMPT = (
    "Ты ведёшь Telegram-канал приложения «Кубышка» (личные финансы, копилка на цели). "
    "Пост должен ЧИТАТЬСЯ как единый цельный текст, а не как список шагов — "
    "никаких '1)', '2)', слова 'Заголовок:' или похожей разметки в готовом тексте, "
    "это только описание логики ниже, не формат вывода.\n"
    "Логика поста: сначала, в юмористической, самоироничной форме — понятная "
    "ЖИТЕЙСКАЯ проблема или привычка читателя с деньгами (без упоминания "
    "приложения), в которой читатель узнаёт себя. Дальше, естественным "
    "переходом в том же тексте — что в «Кубышке» есть функция, которая именно "
    "эту проблему решает, в виде юзкейса («теперь можно X»), без тикетов и "
    "технических терминов.\n"
    "ВАЖНО про длину: большинство постов — КОРОТКИЕ, 2-3 предложения суммарно "
    "(включая заголовок). Разворачивайся подробнее, только если тема реально "
    "этого требует, и то нечасто — не пиши длинный текст по умолчанию.\n"
    "Пиши просто и живо — читатель не обязан разбираться в финансовых терминах, "
    "избегай канцелярита и сложных слов. Иногда (не в каждом посте) можно "
    "по-доброму назвать читателей необычным, тёплым прозвищем в духе "
    "«кубышкины» — так, как больше никто не называет.\n"
    "Шутки мягкие, без сарказма и без осуждения читателя. Начни с короткой "
    "цепляющей строки-заголовка (эмодзи уместны) прямо в тексте поста, без "
    "префикса. Не выдумывай цифры и факты, которых нет в теме."
)


# R-COST: LLMConfig.reasoning_effort="minimal" убирает налог на скрытые
# reasoning-токены — без него 400 не хватало (см. память проекта/коммит
# про пустые ответы), с "minimal" хватает даже меньшего бюджета с запасом.
_POST_MAX_TOKENS = 500

# По просьбе Кирилла: боту, который общается в чатах, "некуда вести" людей
# без канала, а канал максимум может приглашать в бета-тест. Не на каждый
# пост (выглядело бы навязчиво/рекламно) — с небольшой вероятностью.
_BETA_INVITE_PROBABILITY = 0.2


def _maybe_beta_invite(beta_invite_url: str) -> str:
    if not beta_invite_url or random.random() > _BETA_INVITE_PROBABILITY:
        return ""
    return f"\n\n👋 Хочешь попробовать раньше всех — залетай в бета-тест: {beta_invite_url}"


def _write_post(llm: LLMClient, topic: str, beta_invite_url: str = "") -> str:
    post = llm.chat(
        [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": f"Тема поста:\n{topic}"},
        ],
        max_tokens=_POST_MAX_TOKENS,
    )
    return post + _maybe_beta_invite(beta_invite_url)


def generate_next_post(
    llm: LLMClient,
    *,
    queue_path: str,
    changelog_path: str,
    used_state_path: str,
    docs_path: str,
    dry_run: bool = False,
    beta_invite_url: str = "",
) -> str:
    """dry_run=True — для /preview: генерирует текст, НЕ трогая состояние
    (не выкидывает тему из очереди, не помечает запись changelog
    использованной), чтобы предпросмотр не "тратил" реальный контент."""
    topic = peek_next_topic(queue_path) if dry_run else pop_next_topic(queue_path)
    if topic:
        return _write_post(llm, topic, beta_invite_url)

    entry = next_unused_entry(changelog_path, used_state_path)
    if entry:
        if not dry_run:
            mark_title_used(used_state_path, entry["title"])
        topic = f"{entry['title']}\n\n{entry['body']}"
        return _write_post(llm, topic, beta_invite_url)

    # R-COST: очередь и changelog исчерпаны — раньше тут было 2 вызова LLM
    # (сначала "придумай тему", потом отдельно "напиши пост по теме"), хотя
    # это один и тот же контекст проекта дважды в токенах. Одним вызовом —
    # просим модель самой выбрать тему и сразу написать пост, тем же
    # системным промптом, что и обычная генерация по теме.
    context = load_project_context(docs_path, max_chars=6000)
    post = llm.chat(
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
    return post + _maybe_beta_invite(beta_invite_url)
