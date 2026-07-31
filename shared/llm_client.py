from __future__ import annotations

import base64

from openai import OpenAI

from shared.config import LLMConfig

_REASONING_MODEL_PREFIXES = ("gpt-5", "o1", "o3", "o4")


def _is_reasoning_model(model: str) -> bool:
    # Модель может прийти с префиксом провайдера (если base_url когда-нибудь
    # снова укажет на OpenRouter или похожий шлюз) — rsplit делает проверку
    # независимой от префикса, но ничего не ломает и для голых имён OpenAI.
    bare_model = model.rsplit("/", 1)[-1]
    return bare_model.startswith(_REASONING_MODEL_PREFIXES)


# R-COST: reasoning_effort="minimal" убирает налог на скрытые reasoning-токены
# в большинстве случаев, но НЕ гарантированно — с большим контекстом (богатые
# доки проекта) модель иногда всё равно гасит весь бюджет на reasoning и
# возвращает пустую строку, причём нестабильно от запуска к запуску (проверено
# реальными вызовами: один и тот же запрос с одним и тем же бюджетом то
# срабатывал, то нет). Поэтому вместо того чтобы закладывать большой бюджет в
# КАЖДЫЙ вызов (дорого), платим за повторный вызов с большим бюджетом только
# в те редкие разы, когда первый попытка вернулась пустой.
_EMPTY_RESPONSE_RETRY_MULTIPLIER = 3
_EMPTY_RESPONSE_RETRY_CEILING = 2000


class LLMClient:
    """Тонкая обёртка над OpenAI. Reasoning-модели (gpt-5*, o1/o3/o4) не
    принимают `max_tokens`/`temperature` — Finik-backend ловил это в проде
    (инцидент 2026-06-07), тот же фикс реализован здесь заново, а не
    унаследован из того репо."""

    def __init__(self, config: LLMConfig | None = None, model_override: str = "") -> None:
        self.config = config or LLMConfig()
        # R-COST: позволяет команд-боту (низкие ставки — внутренний Q&A) сидеть
        # на более дешёвой/быстрой модели, не трогая ту, что настроена для
        # публичного канала (там важнее качество текста, не только цена).
        self._model = model_override or self.config.model
        # base_url="" (не задан в .env) → None, чтобы OpenAI SDK сам подставил
        # свой дефолтный https://api.openai.com/v1, а не отправлял запрос на
        # пустой адрес.
        self.client = OpenAI(
            api_key=self.config.api_key, base_url=self.config.base_url or None, timeout=30.0, max_retries=1
        )

    def chat(
        self, messages: list[dict[str, str]], *, max_tokens: int = 600, temperature: float = 0.7, model: str = ""
    ) -> str:
        model = model or self._model
        answer = self._request(messages, max_tokens=max_tokens, temperature=temperature, model=model)
        if not answer and _is_reasoning_model(model):
            retry_budget = min(max_tokens * _EMPTY_RESPONSE_RETRY_MULTIPLIER, _EMPTY_RESPONSE_RETRY_CEILING)
            if retry_budget > max_tokens:
                answer = self._request(messages, max_tokens=retry_budget, temperature=temperature, model=model)
        return answer

    def describe_image(self, image_path: str, prompt: str, *, max_tokens: int = 400, model: str = "") -> str:
        """Vision-запрос: картинка + текстовый промпт в одном сообщении (формат
        content-частей OpenAI chat completions). Используется для анализа
        фото, прикреплённых к задачам доски (см. team_bot/main.py
        _analyze_and_annotate_photo). Reasoning-модели (gpt-5* и т.п.) уже
        поддерживают vision наравне с обычными — тот же _request ниже."""
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ],
            }
        ]
        return self._request(messages, max_tokens=max_tokens, temperature=0.4, model=model or self._model)

    def search_chat(
        self, messages: list[dict[str, str]], *, max_tokens: int = 1200, model: str = "gpt-4o-search-preview"
    ) -> tuple[str, list[str]]:
        """Запрос с включённым веб-поиском (OpenAI web_search_options) —
        единственный способ в этой кодовой базе реально ПРОВЕРИТЬ факт, а не
        просто спросить модель по памяти (см. team_bot/news_digest.py:
        founder явно просил перепроверять новость и показывать ссылку на
        первоисточник — сайт, а не Telegram-канал). Возвращает (текст,
        список URL источников из annotations) — URL достаём из ответа модели,
        а не парсим текст руками, чтобы не гадать формат ссылки.
        gpt-4o-search-preview не reasoning-модель (другой префикс) — обычный
        max_tokens/без temperature (сама search-preview его не принимает).
        """
        completion = self.client.chat.completions.create(
            model=model,
            messages=messages,
            web_search_options={},
            max_tokens=max_tokens,
        )
        message = completion.choices[0].message
        text = message.content or ""
        urls: list[str] = []
        for annotation in getattr(message, "annotations", None) or []:
            citation = getattr(annotation, "url_citation", None)
            url = getattr(citation, "url", None) if citation else None
            if url and url not in urls:
                urls.append(url)
        return text, urls

    def transcribe(self, file_path: str, *, language: str = "ru") -> str:
        """Транскрибация аудио/видео через Whisper (whisper-1). Принимает mp3,
        mp4, mpeg, mpga, m4a, wav, webm, ogg напрямую — Whisper сам вытаскивает
        звуковую дорожку из видео-контейнеров, отдельная распаковка через
        ffmpeg не нужна. Лимит OpenAI — 25 МБ на файл (см. team_bot/main.py —
        там же проверка лимита Telegram Bot API на скачивание, 20 МБ, который
        обычно оказывается уже этого)."""
        with open(file_path, "rb") as audio_file:
            transcript = self.client.audio.transcriptions.create(
                model="whisper-1", file=audio_file, language=language
            )
        return transcript.text

    def _request(self, messages: list[dict], *, max_tokens: int, temperature: float, model: str) -> str:
        kwargs: dict = {"model": model, "messages": messages}
        if _is_reasoning_model(model):
            kwargs["max_completion_tokens"] = max_tokens
            if self.config.reasoning_effort:
                # Снижает шанс пустого ответа (см. LLMConfig.reasoning_effort),
                # но не гарантирует — отсюда retry в chat() выше.
                kwargs["reasoning_effort"] = self.config.reasoning_effort
        else:
            kwargs["max_tokens"] = max_tokens
            kwargs["temperature"] = temperature
        completion = self.client.chat.completions.create(**kwargs)
        return completion.choices[0].message.content or ""
