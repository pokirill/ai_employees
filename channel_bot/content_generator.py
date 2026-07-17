from __future__ import annotations

import random
from dataclasses import dataclass, field

from channel_bot.changelog_entries import load_used_titles, mark_title_used, parse_changelog_entries
from channel_bot.content_queue import peek_next_topic, pop_next_topic
from channel_bot.feedback_store import load_feedback
from channel_bot.post_history import load_recent_summaries
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
_SYSTEM_PROMPT_BODY = (
    "Ты ведёшь Telegram-канал приложения «Кубышка» (личные финансы, копилка на цели). "
    "Пост должен ЧИТАТЬСЯ как единый цельный текст, а не как список шагов — "
    "никаких '1)', '2)', слова 'Заголовок:' или похожей разметки в готовом тексте, "
    "это только описание логики ниже, не формат вывода.\n"
    "ОБЯЗАТЕЛЬНО раздели пост на 3 абзаца пустой строкой между ними (перенос "
    "строки дважды) — это про ОФОРМЛЕНИЕ, а не про нумерацию или списки: "
    "1) цепляющее начало/заголовок, 2) основная часть — раскрытие темы, "
    "3) заключение/вопрос к читателю. Каждый абзац — цельный текст без своей "
    "внутренней разметки, просто три визуально отдельных блока подряд.\n"
    "Логика поста ЗАВИСИТ от темы:\n"
    "— Если тема — конкретная функция/фича приложения: сначала, в "
    "юмористической, самоироничной форме — понятная ЖИТЕЙСКАЯ проблема или "
    "привычка читателя с деньгами (без упоминания приложения), в которой "
    "читатель узнаёт себя. Дальше, естественным переходом в том же тексте — "
    "что в «Кубышке» есть функция, которая именно эту проблему решает, в виде "
    "юзкейса («теперь можно X»), без тикетов и технических терминов.\n"
    "— Если тема — личная история, наблюдение или случай из жизни (не "
    "привязанные к конкретной функции): если в теме описан конкретный "
    "реальный случай — пиши от первого лица, как дневниковую запись; если "
    "тема — общее поведенческое наблюдение о деньгах без конкретного случая "
    "(забытые подписки, траты по умолчанию, недостигнутое чувство от уже "
    "достигнутой цели и т.п.) — пиши в форме «а вы замечали, что...», не "
    "выдумывая от себя конкретный вымышленный случай как будто он реально "
    "произошёл. НЕ нужно искусственно приплетать функцию приложения, если "
    "сама тема этого не просит. Такие посты держат канал живым, не только "
    "рекламным.\n"
    "НИКОГДА не упоминай внутреннюю механику удержания/уведомлений — "
    "«стрик», «риск стрика», алгоритмы пушей, антиотточные механизмы и "
    "подобное. Это не то, о чём должен знать читатель, даже переформулированное. "
    "Если тема целиком про такую внутреннюю механику — найди в ней "
    "ПОЛЬЗОВАТЕЛЬСКУЮ выгоду и пиши только про неё, не касаясь того, как это "
    "устроено внутри.\n"
    "НИКОГДА не упоминай статус разработки/тестирования/сборки — что "
    "запушено, что на очереди, готовность билда, QA, код-ревью, «гейт N», "
    "приоритеты вида P0/P1, номера/хэши коммитов, количество прошедших "
    "тестов. Это внутренний процесс команды, а не то, что видит пользователь.\n"
    "НИКОГДА не копируй в пост внутренние технические идентификаторы из "
    "темы буквально — имена полей/переменных (например person_id, "
    "display_name), названия функций/классов, пути API/эндпоинты, названия "
    "внутренних сервисов. Если тема содержит такие детали — перескажи "
    "СМЫСЛ простыми словами пользователя, не называя сам технический термин.\n"
    "Тема поста может описывать НЕСКОЛЬКО разных изменений сразу (запись из "
    "инженерного лога часто так устроена) — выбери из них ОДНО, самое "
    "понятное и интересное читателю, и пиши только про него, полностью "
    "игнорируя остальные пункты. Не пытайся уместить всё в один пост.\n"
    "Пиши просто и живо — читатель не обязан разбираться в финансовых терминах, "
    "избегай канцелярита и сложных слов. Иногда (не в каждом посте) можно "
    "по-доброму назвать читателей необычным, тёплым прозвищем в духе "
    "«кубышкины» — так, как больше никто не называет.\n"
    "Шутки мягкие, без сарказма и без осуждения читателя (осуждать/подкалывать "
    "можно только себя саму как канал или воображаемого смм-щика «Кубышки» — "
    "так шутит Авиасейлс, читателя это не касается). Начни с короткой "
    "цепляющей строки-заголовка (эмодзи уместны) прямо в тексте поста, без "
    "префикса — но не всегда одним и тем же приёмом: чередуй разоблачение "
    "расхожего мнения, риторический вопрос, бытовую реплику/диалог, честное "
    "признание своей мелкой слабости, сравнение через понятную метафору, а "
    "иногда — весёлое абсурдное преувеличение или неожиданное сравнение (в "
    "духе Авиасейлс), если тема это позволяет. Не начинай пост с «Знаете ли "
    "вы...» — это заезженный, раздражающий зачин.\n"
    "Не пиши скучно и сухо, как техническое объявление для разработчиков, — "
    "живой пост важнее, чем точный и полный пересказ изменения.\n"
    "После того как раскрыл фичу — не разжёвывай её длинным комментарием "
    "дальше, дай ей «говорить самой за себя» и закругляйся.\n"
    "НИКОГДА не используй эти клише и похожие на них по духу фразы: «по цене "
    "чашки кофе», «с нуля» без конкретики, «прямо сейчас/сегодня» как "
    "искусственную спешку, любой намёк на ограниченное по времени предложение "
    "или дефицит, «знаете ли вы, что», «премиальное/люксовое качество», "
    "«эффективный и действенный», «сделаем всё возможное», «и вы скажете "
    "спасибо», «побалуйте себя», голословное «нам можно доверять» без "
    "конкретного доказательства. Абстрактную похвалу всегда заменяй "
    "конкретной деталью или фактом.\n"
    "Гигиена текста: одно предложение — одна мысль, без канцелярита. Убирай "
    "слова-паразиты и лишние повторы («так как», «очень», «кстати», «в "
    "общем», лишние «наш/ваш/мой»). Не перехваливай функцию — если весь "
    "остальной канал шутит и говорит на равных с читателем, а один пост "
    "вдруг превращается в чистый питч, это режет глаз и выглядит рекламой.\n"
    "Заканчивай пост коротким вопросом или предложением поделиться своим "
    "опытом/фото в комментариях — это должно быть по существу темы поста, а "
    "не общий призыв «попробуйте прямо сейчас».\n"
    "Не выдумывай цифры и факты, которых нет в теме."
)

# Вынесено ОТДЕЛЬНО от _SYSTEM_PROMPT_BODY и всегда добавляется ПОСЛЕДНИМ (см.
# _build_system_prompt) — namespace-фидбек от команды (см. ниже) тоже
# встраивается между телом промпта и этим правилом, а не после него. Урок с
# этой же сессии, поймано дважды: любое новое правило, добавленное ПОСЛЕ
# хардкапа длины, размывает его и пост снова начинает расти (проверено на
# реальных постах). Финальная позиция — единственная защита от повторения.
_LENGTH_CAP_RULE = (
    "САМОЕ ВАЖНОЕ ПРАВИЛО, ВАЖНЕЕ ВСЕХ ОСТАЛЬНЫХ ВЫШЕ: весь пост, включая "
    "заголовок и ВСЕ 3 абзаца вместе, — НЕ БОЛЬШЕ 350 СИМВОЛОВ. Это жёсткий "
    "лимит, не пожелание. Требование в 3 абзаца НЕ означает 3 полноценных "
    "предложения-абзаца — при таком лимите каждый абзац обычно это одна "
    "короткая фраза, а не 2-3 предложения. Прежде чем ответить, посчитай "
    "символы в черновике у себя в голове (складывая все 3 абзаца) — если "
    "больше 350, сокращай КАЖДЫЙ абзац до одной короткой фразы, убирай "
    "детали, пока не уложишься. Лучше нарушить любое другое правило выше, "
    "чем это."
)

_SYSTEM_PROMPT = _SYSTEM_PROMPT_BODY + "\n" + _LENGTH_CAP_RULE


def _compose_prompt(body: str, length_rule: str, team_feedback: list[str] | None) -> str:
    """team_feedback — накопленные замечания команды (см. feedback_store.py и
    main.py: /feedback + правки из кнопки "Предложить правки"). Встраиваются
    МЕЖДУ телом промпта и length_rule, чтобы хардкап длины всегда оставался
    последней и самой заметной инструкцией, сколько бы замечаний ни
    накопилось (см. _LENGTH_CAP_RULE — урок с этой же сессии, поймано дважды)."""
    if not team_feedback:
        return body + "\n" + length_rule
    feedback_block = "\n".join(f"— {note}" for note in team_feedback)
    return (
        body
        + "\n\nКОМАНДА ОСТАВИЛА ЭТИ ЗАМЕЧАНИЯ ПО ПРЕДЫДУЩИМ ПОСТАМ — учитывай их "
        "наравне с правилами выше, это реальный фидбек от людей, которые "
        "публикуют посты:\n"
        + feedback_block
        + "\n\n"
        + length_rule
    )


def _build_system_prompt(team_feedback: list[str] | None = None) -> str:
    return _compose_prompt(_SYSTEM_PROMPT_BODY, _LENGTH_CAP_RULE, team_feedback)


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
    "всё, что пользователь не видит и не должен видеть.\n"
    "ВАЖНО: запись может УПОМИНАТЬ отдельную пользовательскую деталь внутри "
    "длинного технического отчёта (гейты/тесты/ревью/приоритеты вроде P0, "
    "коммиты, внутренние идентификаторы) — формальное присутствие такой "
    "детали НЕ делает всю запись подходящей. Если запись В ОСНОВНОМ (по "
    "объёму и заголовку) — отчёт о процессе разработки/тестирования/сборки, "
    "а не описание фичи, отвечай 'нет', даже если где-то внутри есть "
    "пользовательская деталь.\n"
    "При сомнении — 'нет'. Ответь ТОЛЬКО одним словом: 'да' или 'нет'."
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


_VALID_SLOT_CATEGORIES = ("poll", "feature", "personal")


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


def _build_revision_system_prompt(team_feedback: list[str] | None = None) -> str:
    return (
        _build_system_prompt(team_feedback)
        + "\n\nТебе дают уже готовый черновик поста и правку от админа канала "
        "(конкретное замечание, идею или пожелание). Перепиши пост с учётом этой "
        "правки, по-прежнему соблюдая все правила выше (длина, тон, без "
        "внутренней механики и т.д.), КРОМЕ случаев, когда правка админа прямо "
        "противоречит какому-то из этих правил (например, просит убрать "
        "закрывающий вопрос, хотя обычное правило — всегда заканчивать "
        "вопросом) — тогда выполняй ИМЕННО правку админа, это осознанное "
        "разовое исключение для этого конкретного черновика, а не отмена "
        "правила вообще. Если правка расплывчата — истолкуй её в духе "
        "пожеланий читателя канала, а не буквально дословно."
    )


# R-ROBUST: промпт называет лимит длины "важнее всех остальных правил", но
# эмпирически модель иногда всё равно превышает его — особенно когда тема
# бандлит несколько изменений сразу (см. память проекта: 2 из 10 реальных
# генераций по объёмной записи changelog превысили 350 символов ОДНОВРЕМЕННО
# с нарушением правила "выбери ОДНО"). Промпт-инжиниринг не даёт 100%
# соблюдения жёсткого числового лимита — нужен детерминированный код-бэкстоп,
# тот же принцип, что retry-on-empty в shared/llm_client.py для другого
# класса сбоев (там — пустой ответ reasoning-модели, здесь — превышение длины).
# Числа здесь ДОЛЖНЫ совпадать с тем, что написано в самом промпте
# (_LENGTH_CAP_RULE/_INTRO_LENGTH_CAP_RULE) — если меняешь лимит в промпте,
# поменяй и здесь.
_POST_LENGTH_LIMIT_CHARS = 350
_INTRO_LENGTH_LIMIT_CHARS = 700
_LENGTH_RETRY_ATTEMPTS = 2


def _chat_within_length_limit(
    llm: LLMClient, system_prompt: str, user_content: str, *, max_tokens: int, limit_chars: int
) -> str:
    """Просит модель сократить СВОЙ ЖЕ предыдущий ответ (не генерирует с нуля
    заново) — если предыдущая попытка превысила лимит, у модели уже есть
    конкретный текст, который нужно урезать, а не риск снова случайно
    забандлить несколько тем в новой попытке "с нуля"."""
    messages: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
    text = llm.chat(messages, max_tokens=max_tokens)
    for _ in range(_LENGTH_RETRY_ATTEMPTS):
        if len(text) <= limit_chars:
            return text
        messages = messages + [
            {"role": "assistant", "content": text},
            {
                "role": "user",
                "content": (
                    f"Это {len(text)} символов — превышает лимит {limit_chars}. "
                    "Сократи текст, убрав менее важные детали, но сохрани "
                    "структуру абзацев и общий смысл."
                ),
            },
        ]
        text = llm.chat(messages, max_tokens=max_tokens)
    # Попытки исчерпаны — возвращаем последний вариант как есть, не блокируя
    # публикацию полностью; админ всё равно увидит черновик на ревью и может
    # среагировать (реролл/правки), если он всё ещё слишком длинный.
    return text


def revise_post(llm: LLMClient, original: GeneratedPost, feedback: str, *, feedback_path: str = "") -> GeneratedPost:
    """Переписывает текстовый черновик с учётом правки админа (см. кнопку
    "Предложить правки" в main.py). Опросы не ревизуются здесь — main.py
    отсекает эту кнопку для kind="poll" ещё до вызова. feedback_path — путь к
    накопленным замечаниям команды (см. feedback_store.py); main.py сам
    отдельно сохраняет туда этот же feedback ПОСЛЕ вызова, чтобы он повлиял
    на БУДУЩИЕ посты, а не только переписал текущий черновик."""
    team_feedback = load_feedback(feedback_path) if feedback_path else None
    text = _chat_within_length_limit(
        llm,
        _build_revision_system_prompt(team_feedback),
        f"Черновик поста:\n{original.text}\n\nПравка от админа:\n{feedback}",
        max_tokens=_POST_MAX_TOKENS,
        limit_chars=_POST_LENGTH_LIMIT_CHARS,
    )
    return GeneratedPost(kind="text", text=text)


def _user_content_with_history(topic: str, recent_posts: list[str] | None) -> str:
    """recent_posts — контекст (не постоянное правило, в отличие от
    team_feedback), поэтому идёт в user-сообщение, а не в system-промпт: это
    данные о том, что уже сказано, а не инструкция на будущее."""
    content = f"Тема поста:\n{topic}"
    if not recent_posts:
        return content
    history_block = "\n".join(f"- {p}" for p in recent_posts)
    return (
        content
        + "\n\nПоследние опубликованные посты канала (для контекста — НЕ "
        "повторяй те же темы, шутки, сравнения или структуру открытия, что "
        f"уже были):\n{history_block}"
    )


def _generate_post(
    llm: LLMClient,
    topic: str,
    beta_invite_url: str = "",
    force_kind: str | None = None,
    team_feedback: list[str] | None = None,
    recent_posts: list[str] | None = None,
) -> GeneratedPost:
    """force_kind=None — старое вероятностное поведение (см. _POLL_PROBABILITY,
    для необязательного /postnow и /preview). force_kind="poll"/"text" — для
    именованных слотов расписания (см. generate_post_for_category), где формат
    поста задаётся местом в расписании, а не случайностью."""
    wants_poll = force_kind == "poll" or (force_kind is None and random.random() < _POLL_PROBABILITY)
    if wants_poll:
        poll = _write_poll(llm, topic)
        if poll is not None:
            return poll
        # Модель не ответила строгим форматом — не проваливаем публикацию
        # только из-за формата, тихо откатываемся на обычный текстовый пост.
    text = _chat_within_length_limit(
        llm,
        _build_system_prompt(team_feedback),
        _user_content_with_history(topic, recent_posts),
        max_tokens=_POST_MAX_TOKENS,
        limit_chars=_POST_LENGTH_LIMIT_CHARS,
    )
    return GeneratedPost(kind="text", text=text + _maybe_beta_invite(beta_invite_url))


def _next_feature_topic(
    llm: LLMClient, queue_path: str, changelog_path: str, used_state_path: str, docs_path: str, *, dry_run: bool
) -> tuple[str, bool]:
    """Тема для фичи-поста: очередь → changelog → тема сочиняется по общему
    контексту проекта. Второй элемент — has_specific_topic: False только в
    последнем случае, когда очередь и changelog исчерпаны — на такой теме
    осознанно не форсируем опрос (см. вызывающий код), вопрос вышел бы
    слишком общим без конкретной темы."""
    topic = peek_next_topic(queue_path) if dry_run else pop_next_topic(queue_path)
    if topic:
        return topic, True

    entry = _next_public_changelog_entry(llm, changelog_path, used_state_path, dry_run=dry_run)
    if entry:
        if not dry_run:
            mark_title_used(used_state_path, entry["title"])
        return f"{entry['title']}\n\n{entry['body']}", True

    # R-COST: очередь и changelog исчерпаны — просим модель самой выбрать тему
    # по контексту проекта, тем же системным промптом, что и обычная
    # генерация по теме (один вызов LLM, не два).
    context = load_project_context(docs_path, max_chars=6000)
    topic = (
        "Очередь тем и записи AI_CHANGELOG.md закончились — выбери сама "
        "интересную тему по контексту проекта ниже и сразу напиши пост по ней "
        f"(без промежуточного описания темы).\n\nКонтекст проекта:\n{context}"
    )
    return topic, False


def generate_next_post(
    llm: LLMClient,
    *,
    queue_path: str,
    changelog_path: str,
    used_state_path: str,
    docs_path: str,
    dry_run: bool = False,
    beta_invite_url: str = "",
    feedback_path: str = "",
    history_path: str = "",
) -> GeneratedPost:
    """dry_run=True — для /preview: генерирует текст, НЕ трогая состояние
    (не выкидывает тему из очереди, не помечает запись changelog
    использованной), чтобы предпросмотр не "тратил" реальный контент.
    Свободный формат (без привязки к теме слота расписания) — см.
    generate_post_for_category для расписания с фиксированной темой на слот.
    feedback_path — накопленные замечания команды, см. feedback_store.py.
    history_path — последние опубликованные посты, см. post_history.py."""
    topic, has_specific_topic = _next_feature_topic(llm, queue_path, changelog_path, used_state_path, docs_path, dry_run=dry_run)
    force_kind = None if has_specific_topic else "text"
    team_feedback = load_feedback(feedback_path) if feedback_path else None
    recent_posts = load_recent_summaries(history_path) if history_path else None
    return _generate_post(llm, topic, beta_invite_url, force_kind=force_kind, team_feedback=team_feedback, recent_posts=recent_posts)


def generate_post_for_category(
    llm: LLMClient,
    category: str,
    *,
    queue_path: str,
    story_queue_path: str,
    changelog_path: str,
    used_state_path: str,
    docs_path: str,
    dry_run: bool = False,
    beta_invite_url: str = "",
    feedback_path: str = "",
    history_path: str = "",
) -> GeneratedPost | None:
    """Для расписания с закреплённой темой на слот (см. main.py/_DAILY_SLOTS):
    category="feature" — обычный проблема→фича текст; category="poll" — тот
    же источник тем, но форсированный опрос; category="personal" — тема
    берётся ТОЛЬКО из отдельной очереди реальных историй (story_queue_path,
    см. /addstory) — вернёт None, если она пуста, а не выдумает историю;
    вызывающий код сам решает, чем заменить пустой слот. feedback_path —
    накопленные замечания команды, см. feedback_store.py. history_path —
    последние опубликованные посты, см. post_history.py."""
    if category not in _VALID_SLOT_CATEGORIES:
        raise ValueError(f"unknown slot category: {category!r}")

    team_feedback = load_feedback(feedback_path) if feedback_path else None
    recent_posts = load_recent_summaries(history_path) if history_path else None

    if category == "personal":
        topic = peek_next_topic(story_queue_path) if dry_run else pop_next_topic(story_queue_path)
        if not topic:
            return None
        return _generate_post(llm, topic, beta_invite_url, force_kind="text", team_feedback=team_feedback, recent_posts=recent_posts)

    topic, has_specific_topic = _next_feature_topic(llm, queue_path, changelog_path, used_state_path, docs_path, dry_run=dry_run)
    if category == "poll" and has_specific_topic:
        return _generate_post(llm, topic, beta_invite_url, force_kind="poll", team_feedback=team_feedback)
    return _generate_post(llm, topic, beta_invite_url, force_kind="text", team_feedback=team_feedback, recent_posts=recent_posts)


# Отдельный, не входящий в _VALID_SLOT_CATEGORIES промпт — закреплённый
# вступительный пост, который прочитает КАЖДЫЙ новый подписчик, это
# "эталонный" разовый пост про продукт целиком, а не про одну фичу/тему из
# очереди. Не участвует в daily-расписании (см. main.py) — вызывается
# вручную через /draft intro | /preview intro | /postnow intro.
_INTRO_SYSTEM_PROMPT_BODY = (
    "Ты пишешь ЗАКРЕПЛЁННЫЙ вступительный пост Telegram-канала приложения "
    "«Кубышка» (личные финансы, копилка на цели). Этот пост будет висеть "
    "закреплённым и его прочитает КАЖДЫЙ новый подписчик как первое "
    "знакомство с каналом и приложением — пиши как тщательно выверенный "
    "эталонный текст, не как обычный пост из ленты.\n"
    "Расскажи простым языком, без канцелярита и без глубокого технического "
    "разбора: кто мы, что за приложение «Кубышка» и какие конкретные "
    "житейские проблемы с деньгами оно решает (накопить на цель, не терять "
    "из виду мелкие траты, понять, куда уходят деньги, и т.п.) — используй "
    "реальные факты и функции из контекста проекта ниже, не выдумывай "
    "ничего, чего там нет.\n"
    "Тон живой и тёплый, с лёгким юмором, но БЕЗ перебора — это лицо канала "
    "для новых людей, а не шутка ради шутки; лёгкая ирония уместна, "
    "кричащий кликбейт и балаган — нет.\n"
    "Структура — 4 отдельных абзаца, разделённых пустой строкой (перенос "
    "строки дважды), каждый абзац цельным текстом без внутренней разметки: "
    "1) короткий цепляющий заголовок, 2) кто мы и какую проблему в целом "
    "решаем (2-3 предложения), 3) что вообще можно найти в этом канале "
    "дальше (коротко, по-настоящему, без обещаний того, чего не будет), "
    "4) мягкое приглашение остаться подписанным, без нажима и без "
    "\"подпишись\" в лоб.\n"
    "Не упоминай статус разработки, внутренние процессы, названия "
    "задач/тикетов, технические детали реализации. Пиши так, будто "
    "рассказываешь другу, который в первый раз услышал про приложение."
)

_INTRO_LENGTH_CAP_RULE = (
    "ЖЁСТКИЙ ЛИМИТ ДЛИНЫ: весь пост целиком, включая заголовок и ВСЕ 4 "
    "абзаца вместе, — НЕ БОЛЬШЕ 700 СИМВОЛОВ. Закреплённому посту можно "
    "чуть больше воздуха, чем обычным постам (там лимит 350), но он всё "
    "равно должен читаться быстро, не как лонгрид. Структура из 4 абзацев "
    "НЕ означает 4 полноценных абзаца по 2-3 предложения — при лимите 700 "
    "каждый абзац обычно 1, максимум 2 коротких предложения. Посчитай "
    "символы в черновике, складывая все абзацы, — если больше 700, сокращай "
    "КАЖДЫЙ абзац, а не убирай сами абзацы. Это жёсткий лимит, не пожелание."
)


def generate_intro_post(llm: LLMClient, docs_path: str, *, beta_invite_url: str = "", feedback_path: str = "") -> GeneratedPost:
    """Разовый закреплённый пост "кто мы" — НЕ берёт тему из очереди/changelog,
    вместо этого получает общий контекст проекта. Никогда не опрос."""
    team_feedback = load_feedback(feedback_path) if feedback_path else None
    context = load_project_context(docs_path, max_chars=6000)
    system_prompt = _compose_prompt(_INTRO_SYSTEM_PROMPT_BODY, _INTRO_LENGTH_CAP_RULE, team_feedback)
    text = _chat_within_length_limit(
        llm,
        system_prompt,
        f"Контекст проекта «Кубышка»:\n{context}",
        max_tokens=900,
        limit_chars=_INTRO_LENGTH_LIMIT_CHARS,
    )
    return GeneratedPost(kind="text", text=text + _maybe_beta_invite(beta_invite_url))
