from __future__ import annotations

from openai import OpenAI

from shared.config import LLMConfig

_REASONING_MODEL_PREFIXES = ("gpt-5", "o1", "o3", "o4")


def _is_reasoning_model(model: str) -> bool:
    # OpenRouter модели идут с префиксом провайдера ("openai/gpt-5-mini") —
    # берём последний сегмент, чтобы проверка работала независимо от префикса.
    bare_model = model.rsplit("/", 1)[-1]
    return bare_model.startswith(_REASONING_MODEL_PREFIXES)


class LLMClient:
    """Тонкая обёртка над OpenRouter (OpenAI-совместимый API). Reasoning-модели
    (gpt-5*, o1/o3/o4) не принимают `max_tokens`/`temperature` — Finik-backend
    ловил это в проде (инцидент 2026-06-07), тот же фикс реализован здесь
    заново, а не унаследован из того репо."""

    def __init__(self, config: LLMConfig | None = None) -> None:
        self.config = config or LLMConfig()
        self.client = OpenAI(api_key=self.config.api_key, base_url=self.config.base_url, timeout=30.0, max_retries=1)

    def chat(self, messages: list[dict[str, str]], *, max_tokens: int = 600, temperature: float = 0.7) -> str:
        model = self.config.model
        kwargs: dict = {"model": model, "messages": messages}
        if _is_reasoning_model(model):
            kwargs["max_completion_tokens"] = max_tokens
        else:
            kwargs["max_tokens"] = max_tokens
            kwargs["temperature"] = temperature
        completion = self.client.chat.completions.create(**kwargs)
        return completion.choices[0].message.content or ""
