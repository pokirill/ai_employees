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
# нафантазировать URL — поэтому пункт 3 ниже жёстко требует выкидывать
# неподтверждённое, а не публиковать с оговоркой "по неподтверждённым данным".
_DIGEST_SYSTEM_PROMPT = (
    "Ты — редактор еженедельного дайджеста финансовых новостей для команды "
    "продукта «Кубышка» (приложение для планирования личного бюджета). Тебе "
    "дают сырые посты за неделю из нескольких Telegram-каналов про экономику "
    "и финансы. Задача:\n"
    "1. Сгруппируй посты в 5-8 РАЗНЫХ новостных сюжетов (не постов) — если "
    "несколько каналов пишут об одном и том же событии, это ОДИН пункт.\n"
    "2. Для КАЖДОГО сюжета — веб-поиском найди и подтверди факт на "
    "независимом первоисточнике (сайт СМИ/регулятора: РБК, Коммерсантъ, "
    "Интерфакс, ЦБ РФ, Forbes и т.п.), НЕ на самих Telegram-каналах.\n"
    "3. Если факт НЕ подтверждается независимым источником или ты не "
    "уверен(а) в точности цифр — ВЫКИНЬ этот пункт целиком. Не публикуй "
    "с оговорками вроде «по неподтверждённым данным».\n"
    "4. Формат ответа — Telegram HTML (НЕ markdown): каждый пункт как "
    "«<b>Короткий заголовок</b>\\n1-2 предложения сути простым языком\\n"
    "<a href=\"URL первоисточника\">Источник</a>», пункты разделяй пустой "
    "строкой. Ссылка — прямой URL сайта-источника, никогда не t.me/... .\n"
    "5. Пиши по-русски живым языком, без канцелярита, без вступления и "
    "заключения — сразу список пунктов."
)


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

    raw_posts_text = "\n\n---\n\n".join(f"[@{p.channel}, {p.posted_at:%d.%m %H:%M}]\n{p.text}" for p in posts)
    messages = [
        {"role": "system", "content": _DIGEST_SYSTEM_PROMPT},
        {"role": "user", "content": f"Посты за неделю ({len(posts)} шт. из {len(channels)} каналов):\n\n{raw_posts_text}"},
    ]
    try:
        text, source_urls = llm.search_chat(messages, max_tokens=1800)
    except Exception:
        logger.exception("News digest LLM call failed")
        return "📰 Еженедельный дайджест: не получилось собрать сводку — ошибка LLM/поиска, смотри логи бота."

    if not text.strip():
        return "📰 Еженедельный дайджест: после фактчека не осталось ни одной подтверждённой новости за эту неделю."

    logger.info("News digest built: %d posts in, %d cited sources", len(posts), len(source_urls))
    header = f"📰 <b>Дайджест недели</b> — экономика и финансы, {len(posts)} постов из {len(channels)} каналов, с проверкой источников:\n\n"
    return header + text
