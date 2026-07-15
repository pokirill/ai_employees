from __future__ import annotations

import random
from dataclasses import dataclass, field

from channel_bot.changelog_entries import load_used_titles, mark_title_used, parse_changelog_entries
from channel_bot.content_queue import peek_next_topic, pop_next_topic
from shared.docs_context import load_project_context
from shared.llm_client import LLMClient


@dataclass
class GeneratedPost:
    """kind="text" — обычный пост (.text). kind="poll" — Telegram-опрос
    (.question + .options, 2-10 вариантов, см. Bot API send_poll)."""

    kind: str
    text: str = ""
    question: str = ""
    options: list[str] = field(default_factory=list)

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
    "Логика поста ЗАВИСИТ от темы:\n"
    "— Если тема — конкретная функция/фича приложения: сначала, в "
    "юмористической, самоироничной форме — понятная ЖИТЕЙСКАЯ проблема или "
    "привычка читателя с деньгами (без упоминания приложения), в которой "
    "читатель узнаёт себя. Дальше, естественным переходом в том же тексте — "
    "что в «Кубышке» есть функция, которая именно эту проблему решает, в виде "
    "юзкейса («теперь можно X»), без тикетов и технических терминов.\n"
    "— Если тема — личная история, наблюдение или случай из жизни (не "
    "привязанные к конкретной функции): просто напиши живой личный текст от "
    "первого лица, как дневниковую запись, — НЕ нужно искусственно "
    "приплетать функцию приложения, если сама тема этого не просит. Такие "
    "посты держат канал живым, не только рекламным.\n"
    "НИКОГДА не упоминай внутреннюю механику удержания/уведомлений — "
    "«стрик», «риск стрика», алгоритмы пушей, антиотточные механизмы и "
    "подобное. Это не то, о чём должен знать читатель, даже переформулированное. "
    "Если тема целиком про такую внутреннюю механику — найди в ней "
    "ПОЛЬЗОВАТЕЛЬСКУЮ выгоду и пиши только про неё, не касаясь того, как это "
    "устроено внутри.\n"
    "НИКОГДА не упоминай статус разработки/тестирования/сборки — что "
    "запушено, что на очереди, готовность билда, QA, код-ревью. Это "
    "внутренний процесс команды, а не то, что видит пользователь.\n"
    "Тема поста может описывать НЕСКОЛЬКО разных изменений сразу (запись из "
    "инженерного лога часто так устроена) — выбери из них ОДНО, самое "
    "понятное и интересное читателю, и пиши только про него, полностью "
    "игнорируя остальные пункты. Не пытайся уместить всё в один пост.\n"
    "ЖЁСТКОЕ ОГРАНИЧЕНИЕ ДЛИНЫ: весь пост, включая заголовок, — НЕ БОЛЬШЕ "
    "350 СИМВОЛОВ. Это не пожелание, а жёсткий лимит — посчитай символы перед "
    "ответом. Разворачивайся подробнее (но всё равно в разумных пределах), "
    "только если тема реально этого требует, и то нечасто.\n"
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

# По просьбе Кирилла: разные форматы постов, не только текст — иногда опрос.
# Небольшая вероятность, чтобы не превратить канал в бесконечные голосования.
_POLL_PROBABILITY = 0.15
_POLL_MAX_TOKENS = 200

_POLL_SYSTEM_PROMPT = (
    "Ты ведёшь Telegram-канал приложения «Кубышка» (личные финансы). Придумай "
    "короткий вовлекающий опрос для читателей по теме ниже — по-доброму "
    "любопытный, не сложный, без осуждения. Ответь СТРОГО в этом формате, "
    "без какого-либо другого текста:\n"
    "ВОПРОС: <вопрос, не длиннее одного предложения>\n"
    "ВАРИАНТ: <вариант ответа, коротко>\n"
    "ВАРИАНТ: <вариант ответа, коротко>\n"
    "(всего 2-5 строк ВАРИАНТ, каждая — до нескольких слов)"
)


def _parse_poll_response(raw: str) -> tuple[str, list[str]] | None:
    question = ""
    options: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.upper().startswith("ВОПРОС:"):
            question = line.split(":", 1)[1].strip()
        elif line.upper().startswith("ВАРИАНТ:"):
            option = line.split(":", 1)[1].strip()
            if option:
                options.append(option)
    if not question or len(options) < 2:
        return None
    return question, options[:10]  # Telegram-опрос принимает максимум 10 вариантов


def _maybe_beta_invite(beta_invite_url: str) -> str:
    if not beta_invite_url or random.random() > _BETA_INVITE_PROBABILITY:
        return ""
    return f"\n\n👋 Хочешь попробовать раньше всех — залетай в бета-тест: {beta_invite_url}"


# Записи AI_CHANGELOG.md об этой внутренней механике не должны становиться
# ТЕМОЙ поста вообще (не просто переформулироваться) — поймано на реальном
# случае: пост про "стрик риск"-пуши раскрыл читателям внутреннюю механику
# удержания, хотя формулировка формально была "юзкейсом". Дешёвый быстрый
# пре-фильтр для очевидных случаев — но список ключевых слов НЕ может
# перечислить все внутренние темы (AI_CHANGELOG.md — инженерный лог, там
# полно записей про аналитику/метрики/рефакторинг без явных "стоп-слов",
# см. _is_user_facing ниже для общего случая).
_FORBIDDEN_TOPIC_KEYWORDS = (
    "стрик риск",
    "streak risk",
    "streak_risk",
    "риск оттока",
    "антиотток",
    "engagement push",
)


def _is_internal_only(title: str, body: str) -> bool:
    text = f"{title}\n{body}".lower()
    return any(keyword in text for keyword in _FORBIDDEN_TOPIC_KEYWORDS)


# R-COST: дешёвый классификатор (max_tokens=10) — общий предохранитель поверх
# keyword-фильтра. Поймано реальным случаем: запись "BE-METRICS-K —
# виральность измерима" ни разу не содержала ни одного стоп-слова, но это
# ровно такая же внутренняя техническая деталь (аналитика/метрики), как и
# "стрик риск" — enumerable-блоклист принципиально не может покрыть все
# похожие случаи в инженерном логе на 250+ записей, нужна общая проверка.
_TOPIC_CLASSIFIER_PROMPT = (
    "Ты решаешь, подходит ли запись из ВНУТРЕННЕГО инженерного changelog для "
    "ПУБЛИЧНОГО поста в Telegram-канале приложения «Кубышка». Подходит — "
    "если она описывает видимую пользователю функцию или изменение в самом "
    "приложении. НЕ подходит — если это внутренняя механика: аналитика, "
    "метрики, инструментация, код-ревью, рефакторинг, тесты, бэкенд/API-детали, "
    "миграции, антиотточные или удерживающие механизмы, производительность — "
    "всё, что пользователь не видит и не должен видеть. При сомнении — 'нет'. "
    "Ответь ТОЛЬКО одним словом: 'да' или 'нет'."
)


def _is_user_facing(llm: LLMClient, title: str, body: str) -> bool:
    verdict = llm.chat(
        [
            {"role": "system", "content": _TOPIC_CLASSIFIER_PROMPT},
            {"role": "user", "content": f"{title}\n\n{body}"},
        ],
        max_tokens=10,
        temperature=0.0,
    )
    return "да" in verdict.lower()


def _next_public_changelog_entry(
    llm: LLMClient, changelog_path: str, used_state_path: str, *, dry_run: bool
) -> dict[str, str] | None:
    """Как next_unused_entry, но пропускает записи о внутренней механике,
    которые не должны попадать в публичные посты вообще — не просто
    переформулироваться, а не рассматриваться как тема поста в принципе.
    Сначала дешёвый keyword-фильтр (_is_internal_only) для очевидных
    случаев, потом LLM-классификатор (_is_user_facing) как общий
    предохранитель. dry_run=True (см. /preview) пропускает такие записи для
    выбора темы, но не помечает их использованными — предпросмотр не должен
    ничего мутировать."""
    used = load_used_titles(used_state_path)
    for entry in parse_changelog_entries(changelog_path):
        if entry["title"] in used:
            continue
        if _is_internal_only(entry["title"], entry["body"]) or not _is_user_facing(llm, entry["title"], entry["body"]):
            if not dry_run:
                mark_title_used(used_state_path, entry["title"])
            continue
        return entry
    return None


def _write_poll(llm: LLMClient, topic: str) -> GeneratedPost | None:
    raw = llm.chat(
        [
            {"role": "system", "content": _POLL_SYSTEM_PROMPT},
            {"role": "user", "content": f"Тема:\n{topic}"},
        ],
        max_tokens=_POLL_MAX_TOKENS,
    )
    parsed = _parse_poll_response(raw)
    if parsed is None:
        return None
    question, options = parsed
    return GeneratedPost(kind="poll", question=question, options=options)


def _generate_post(llm: LLMClient, topic: str, beta_invite_url: str = "") -> GeneratedPost:
    if random.random() < _POLL_PROBABILITY:
        poll = _write_poll(llm, topic)
        if poll is not None:
            return poll
        # Модель не ответила строгим форматом — не проваливаем публикацию
        # только из-за формата, тихо откатываемся на обычный текстовый пост.
    text = llm.chat(
        [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": f"Тема поста:\n{topic}"},
        ],
        max_tokens=_POST_MAX_TOKENS,
    )
    return GeneratedPost(kind="text", text=text + _maybe_beta_invite(beta_invite_url))


def generate_next_post(
    llm: LLMClient,
    *,
    queue_path: str,
    changelog_path: str,
    used_state_path: str,
    docs_path: str,
    dry_run: bool = False,
    beta_invite_url: str = "",
) -> GeneratedPost:
    """dry_run=True — для /preview: генерирует текст, НЕ трогая состояние
    (не выкидывает тему из очереди, не помечает запись changelog
    использованной), чтобы предпросмотр не "тратил" реальный контент."""
    topic = peek_next_topic(queue_path) if dry_run else pop_next_topic(queue_path)
    if topic:
        return _generate_post(llm, topic, beta_invite_url)

    entry = _next_public_changelog_entry(llm, changelog_path, used_state_path, dry_run=dry_run)
    if entry:
        if not dry_run:
            mark_title_used(used_state_path, entry["title"])
        topic = f"{entry['title']}\n\n{entry['body']}"
        return _generate_post(llm, topic, beta_invite_url)

    # R-COST: очередь и changelog исчерпаны — раньше тут было 2 вызова LLM
    # (сначала "придумай тему", потом отдельно "напиши пост по теме"), хотя
    # это один и тот же контекст проекта дважды в токенах. Одним вызовом —
    # просим модель самой выбрать тему и сразу написать пост, тем же
    # системным промптом, что и обычная генерация по теме. Опрос в этой ветке
    # намеренно не делаем — без конкретной темы вопрос вышел бы слишком общим.
    context = load_project_context(docs_path, max_chars=6000)
    text = llm.chat(
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
    return GeneratedPost(kind="text", text=text + _maybe_beta_invite(beta_invite_url))
