from __future__ import annotations

from openai import OpenAI

from shared.config import LLMConfig

_REASONING_MODEL_PREFIXES = ("gpt-5", "o1", "o3", "o4")


def _is_reasoning_model(model: str) -> bool:
    # Модель может прийти с префиксом провайдера (если base_url когда-нибудь
    # снова укажет на OpenRouter или похожий шлюз) — rsplit делает проверку
    # независимой от префикса, но ничего не ломает и для голых имён OpenAI.
    bare_model = model.rsplit("/", 1)[-1]
    return bare_model.startswith(_REASONING_MODEL_PREFIXES)


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
        kwargs: dict = {"model": model, "messages": messages}
        if _is_reasoning_model(model):
            kwargs["max_completion_tokens"] = max_tokens
        else:
            kwargs["max_tokens"] = max_tokens
            kwargs["temperature"] = temperature
        completion = self.client.chat.completions.create(**kwargs)
        return completion.choices[0].message.content or ""
