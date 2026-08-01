from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
from bs4 import BeautifulSoup

from shared.llm_client import LLMClient

logger = logging.getLogger("team_bot.news_digest")

_PREVIEW_URL = "https://t.me/s/{channel}"


@dataclass
class ChannelPost:
    channel: str
    text: str
    posted_at: datetime


def _parse_post_datetime(iso_str: str) -> datetime | None:
    try:
        return datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except ValueError:
        return None


def fetch_channel_posts(channel: str, since: datetime, *, client: httpx.Client) -> list[ChannelPost]:
    """Публичный веб-превью канала (t.me/s/<channel>) — работает БЕЗ токена/
    авторизации для любого публичного канала (в отличие от Bot API, которому
    нужно быть админом канала, чтобы читать историю). Не даёт полную неделю
    за один запрос при высокочастотных каналах (нет пагинации назад без
    доп. параметра `?before=<msg_id>`, отдаёт последние ~20 постов) — для
    еженедельного дайджеста-инсайтов этого достаточно, это не архив."""
    try:
        resp = client.get(_PREVIEW_URL.format(channel=channel), timeout=15.0)
        resp.raise_for_status()
    except httpx.HTTPError:
        logger.exception("Failed to fetch preview for channel @%s", channel)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    posts: list[ChannelPost] = []
    for wrap in soup.select(".tgme_widget_message_wrap"):
        time_el = wrap.select_one(".tgme_widget_message_date time")
        if not time_el or not time_el.get("datetime"):
            continue
        posted_at = _parse_post_datetime(time_el["datetime"])
        if not posted_at or posted_at < since:
            continue
        text_el = wrap.select_one(".tgme_widget_message_text")
        text = text_el.get_text("\n", strip=True) if text_el else ""
        if not text:
            continue
        posts.append(ChannelPost(channel=channel, text=text, posted_at=posted_at))
    return posts


def collect_weekly_posts(channels: tuple[str, ...], since: datetime) -> list[ChannelPost]:
    posts: list[ChannelPost] = []
    with httpx.Client(headers={"User-Agent": "Mozilla/5.0 (compatible; KubyshkaTeamBot/1.0)"}) as client:
        for channel in channels:
            posts.extend(fetch_channel_posts(channel, since, client=client))
    return posts


# R-FACTCHECK: founder явно просил (1) перепроверять новость, не пересказывать
# Telegram-канал как есть, (2) показывать ссылку на первоисточник — САЙТ, не
# сам ТГ-канал. web_search_options в LLMClient.search_chat даёт модели реально
# сходить в интернет и вернуть проверенные ссылки (annotations), а не
# нафантазировать URL — поэтому пункт про фактчек ниже жёстко требует выкидывать
# неподтверждённое, а не публиковать с оговоркой "по неподтверждённым данным".
#
# R-RELEVANCE (после первого боевого прогона 01.08.2026): дайджест выбрал
# макро-новости (интервенции Банка Японии, обыски у крипто-обменников,
# закрытие МСП, недвижимость в Черногории) — читать интересно, но применить
# к своему бюджету нечего. Founder явно указал на образец того, что нужно:
# пост "после зарплаты" с процентами о поведении людей с деньгами — то, что
# читатель может примерить на себя. Приоритет ниже теперь явно закреплён в
# промпте, а не оставлен на усмотрение модели.
_DIGEST_SYSTEM_PROMPT = (
    "Ты — редактор еженедельного дайджеста для команды продукта «Кубышка» "
    "(приложение для планирования личного бюджета). Тебе дают сырые посты "
    "за неделю из нескольких Telegram-каналов про экономику и финансы.\n\n"
    "ГЛАВНЫЙ КРИТЕРИЙ ОТБОРА: дайджест должен быть ПРИМЕНИМЫМ к жизни "
    "человека, который сам ведёт свой бюджет — а не сборником "
    "макроэкономических новостей, даже интересных.\n\n"
    "В ПРИОРИТЕТЕ (бери в первую очередь):\n"
    "- поведенческие инсайты про деньги: как люди тратят/копят после "
    "зарплаты, привычки, статистика опросов и исследований о личных "
    "финансах — то, что читатель может примерить на себя и сказать "
    "«о, это как у меня» или «надо тоже так попробовать»;\n"
    "- новости, которые НАПРЯМУЮ и ПРАКТИЧНО влияют на кошелёк обычного "
    "человека: ставки по вкладам/кредитам, изменения в законах о доходах "
    "физлиц и налогах, доступность банковских сервисов, которыми люди "
    "пользуются каждый день.\n"
    "НИЗКИЙ ПРИОРИТЕТ (включай, только если есть явная личная польза "
    "рядовому читателю — обычно нет):\n"
    "- геополитика, макроэкономика уровня действий центробанков других "
    "стран, международная торговля, зарубежная недвижимость, корпоративные "
    "скандалы и уголовные дела без прямой связи с деньгами читателя. Если "
    "не можешь в одном предложении объяснить, что читателю с этим делать "
    "в своём бюджете — не бери этот сюжет вообще, даже если он крупный.\n\n"
    "Дальше:\n"
    "1. Сгруппируй отобранные посты в 5-8 РАЗНЫХ сюжетов (не постов) — если "
    "несколько каналов пишут об одном и том же, это ОДИН пункт.\n"
    "2. Для каждого сюжета — веб-поиском проверь факт/цифры на независимом "
    "источнике (сайт СМИ/регулятора/исследовательской компании — РБК, "
    "Коммерсантъ, Интерфакс, ЦБ РФ, Forbes, сайт автора опроса и т.п.), "
    "НЕ на самом Telegram-канале, который это пересказал. Для "
    "поведенческой статистики/опроса источником может быть сама "
    "организация, которая его провела (это и есть первоисточник для "
    "такого жанра) — не требуй для неё ВТОРОГО независимого подтверждения.\n"
    "3. Если факт НЕ подтверждается ни одним внешним источником или ты не "
    "уверен(а) в точности цифр — ВЫКИНЬ этот пункт целиком. Не публикуй "
    "с оговорками вроде «по неподтверждённым данным».\n"
    "4. Формат ответа — Telegram HTML (НЕ markdown): каждый пункт как "
    "«<b>Короткий заголовок</b>\\n1-2 предложения сути простым языком\\n"
    "<a href=\"URL первоисточника\">Источник</a>», пункты разделяй пустой "
    "строкой. Ссылка — прямой URL сайта-источника, никогда не t.me/... .\n"
    "5. Пиши по-русски живым языком, без канцелярита, без вступления и "
    "заключения — сразу список пунктов."
)


# R-RATELIMIT: боевой прогон на 5 каналов за неделю (~90 постов, полный
# текст) давал запрос на ~26000 токенов при лимите организации в 6000 TPM
# для gpt-4o-search-preview (429 RateLimitError) — дайджест ни разу не
# собирался, только честный fallback "ошибка LLM/поиска". Обрезаем ОБА
# уровня: длину каждого поста (длинные аналитические посты — не длинные
# новостные — не должны съедать весь бюджет в одиночку) и суммарный размер
# текста на входе, оставляя запас под system-промпт и вывод.
#
# Первая версия капов (12000 символов, ÷4 символа/токен) всё ещё падала в
# 429 (6278 против лимита 6000) после того как вырос system-промпт и список
# каналов расширился до 10 — расчёт ÷4 симв/токен был для английского,
# кириллица токенизируется хуже (реально ближе к ÷2.5). Дальше считаем от
# этого более пессимистичного соотношения с явным запасом, а не впритык.
_MAX_CHARS_PER_POST = 350
_MAX_TOTAL_INPUT_CHARS = 5_000
_DIGEST_MAX_OUTPUT_TOKENS = 1400


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def build_news_digest(llm: LLMClient, channels: tuple[str, ...], *, days: int = 7) -> str:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    posts = collect_weekly_posts(channels, since)
    if not posts:
        channel_list = ", ".join(f"@{c}" for c in channels)
        return (
            "📰 Еженедельный дайджест: за последние 7 дней не удалось собрать "
            f"посты ни из одного канала ({channel_list}) — проверь список "
            "каналов в TEAM_NEWS_DIGEST_CHANNELS или сеть."
        )

    post_blocks = []
    total_chars = 0
    truncated_post_count = 0
    for p in posts:
        block = f"[@{p.channel}, {p.posted_at:%d.%m %H:%M}]\n{_truncate(p.text, _MAX_CHARS_PER_POST)}"
        if total_chars + len(block) > _MAX_TOTAL_INPUT_CHARS:
            break
        post_blocks.append(block)
        total_chars += len(block)
    if len(post_blocks) < len(posts):
        truncated_post_count = len(posts) - len(post_blocks)

    raw_posts_text = "\n\n---\n\n".join(post_blocks)
    note = (
        f"\n\n(ещё {truncated_post_count} постов не поместилось в лимит модели — не учтены в этой сводке)"
        if truncated_post_count
        else ""
    )
    messages = [
        {"role": "system", "content": _DIGEST_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"Посты за неделю ({len(post_blocks)} шт. из {len(channels)} каналов):\n\n{raw_posts_text}{note}",
        },
    ]
    try:
        text, source_urls = llm.search_chat(messages, max_tokens=_DIGEST_MAX_OUTPUT_TOKENS)
    except Exception:
        logger.exception("News digest LLM call failed")
        return "📰 Еженедельный дайджест: не получилось собрать сводку — ошибка LLM/поиска, смотри логи бота."

    if not text.strip():
        return "📰 Еженедельный дайджест: после фактчека не осталось ни одной подтверждённой новости за эту неделю."

    logger.info(
        "News digest built: %d/%d posts in (rest truncated), %d cited sources",
        len(post_blocks), len(posts), len(source_urls),
    )
    header = (
        f"📰 <b>Дайджест недели</b> — экономика и финансы, {len(post_blocks)} постов из {len(channels)} каналов, "
        "с проверкой источников:\n\n"
    )
    return header + text
