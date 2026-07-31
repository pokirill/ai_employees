from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from team_bot.news_digest import ChannelPost, build_news_digest, fetch_channel_posts


class _FakeLLM:
    def __init__(self, *, text="", urls=None, raise_error=False):
        self.calls = []
        self._text = text
        self._urls = urls or []
        self._raise_error = raise_error

    def search_chat(self, messages, **kwargs):
        self.calls.append(messages)
        if self._raise_error:
            raise RuntimeError("boom")
        return self._text, self._urls


_SAMPLE_HTML = """
<div class="tgme_widget_message_wrap">
  <div class="tgme_widget_message_date">
    <time datetime="{fresh}"></time>
  </div>
  <div class="tgme_widget_message_text">Свежая новость про ставку ЦБ.</div>
</div>
<div class="tgme_widget_message_wrap">
  <div class="tgme_widget_message_date">
    <time datetime="{stale}"></time>
  </div>
  <div class="tgme_widget_message_text">Старая новость, старше недели.</div>
</div>
<div class="tgme_widget_message_wrap">
  <div class="tgme_widget_message_date">
    <time datetime="{fresh}"></time>
  </div>
  <div class="tgme_widget_message_text"></div>
</div>
"""


def test_fetch_channel_posts_filters_by_date_and_skips_empty_text(monkeypatch):
    now = datetime.now(timezone.utc)
    fresh = (now - timedelta(days=1)).isoformat()
    stale = (now - timedelta(days=10)).isoformat()
    html = _SAMPLE_HTML.format(fresh=fresh, stale=stale)

    def handler(request):
        return httpx.Response(200, text=html)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    since = now - timedelta(days=7)
    posts = fetch_channel_posts("somechannel", since, client=client)

    # Только 1 пост должен пройти: свежий с непустым текстом.
    # Старый (за пределами since) и пустой (нет текста) — отфильтрованы.
    assert len(posts) == 1
    assert posts[0].channel == "somechannel"
    assert "Свежая новость" in posts[0].text


def test_fetch_channel_posts_handles_http_error_gracefully():
    def handler(request):
        raise httpx.ConnectError("no network", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    posts = fetch_channel_posts("somechannel", datetime.now(timezone.utc), client=client)
    assert posts == []


def test_build_news_digest_no_posts_returns_honest_message(monkeypatch):
    monkeypatch.setattr("team_bot.news_digest.collect_weekly_posts", lambda channels, since: [])
    llm = _FakeLLM()
    result = build_news_digest(llm, ("finance_pro_tg",))
    assert "не удалось собрать" in result
    assert "finance_pro_tg" in result
    assert llm.calls == []  # не тратим вызов LLM, если постов вообще нет


def test_build_news_digest_llm_failure_returns_honest_error(monkeypatch):
    fake_post = ChannelPost(channel="x", text="что-то", posted_at=datetime.now(timezone.utc))
    monkeypatch.setattr("team_bot.news_digest.collect_weekly_posts", lambda channels, since: [fake_post])
    llm = _FakeLLM(raise_error=True)
    result = build_news_digest(llm, ("x",))
    assert "не получилось собрать сводку" in result


def test_build_news_digest_empty_llm_text_means_nothing_confirmed(monkeypatch):
    fake_post = ChannelPost(channel="x", text="что-то", posted_at=datetime.now(timezone.utc))
    monkeypatch.setattr("team_bot.news_digest.collect_weekly_posts", lambda channels, since: [fake_post])
    llm = _FakeLLM(text="   ")
    result = build_news_digest(llm, ("x",))
    assert "не осталось ни одной подтверждённой новости" in result


def test_build_news_digest_success_includes_header_and_llm_text(monkeypatch):
    fake_post = ChannelPost(channel="x", text="что-то важное", posted_at=datetime.now(timezone.utc))
    monkeypatch.setattr("team_bot.news_digest.collect_weekly_posts", lambda channels, since: [fake_post])
    llm = _FakeLLM(text="<b>Новость</b>\nСуть.\n<a href=\"https://rbc.ru/x\">Источник</a>", urls=["https://rbc.ru/x"])
    result = build_news_digest(llm, ("x",))
    assert "Дайджест недели" in result
    assert "rbc.ru" in result
    # Промпт фактчека передан модели как system-сообщение.
    assert llm.calls[0][0]["role"] == "system"
    assert "первоисточник" in llm.calls[0][0]["content"]
