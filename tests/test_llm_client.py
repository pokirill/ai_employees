from shared.config import LLMConfig
from shared.llm_client import LLMClient, _is_reasoning_model


def test_reasoning_models_detected():
    assert _is_reasoning_model("gpt-5-mini")
    assert _is_reasoning_model("gpt-5")
    assert _is_reasoning_model("o1-preview")
    assert _is_reasoning_model("o3-mini")


def test_non_reasoning_models_not_flagged():
    assert not _is_reasoning_model("gpt-4.1-mini")
    assert not _is_reasoning_model("gpt-4o")


def _fake_response(content: str):
    return type("Response", (), {"choices": [type("Choice", (), {"message": type("Msg", (), {"content": content})()})]})()


class _FakeCompletions:
    """contents — очередь ответов, один на вызов (по порядку). Последний
    переиспользуется, если вызовов будет больше, чем заготовленных ответов."""

    def __init__(self, contents: list[str] = ("ok",)):
        self.calls: list[dict] = []
        self._contents = list(contents)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        content = self._contents.pop(0) if len(self._contents) > 1 else self._contents[0]
        return _fake_response(content)

    @property
    def received_kwargs(self) -> dict:
        return self.calls[-1]


def _llm_with_fake_client(
    model: str, reasoning_effort: str = "minimal", contents: tuple[str, ...] = ("ok",)
) -> tuple[LLMClient, _FakeCompletions]:
    config = LLMConfig(api_key="x", model=model, base_url="https://api.openai.com/v1", reasoning_effort=reasoning_effort)
    llm = LLMClient(config)
    fake = _FakeCompletions(contents)
    llm.client.chat.completions.create = fake.create
    return llm, fake


def test_reasoning_effort_passed_for_reasoning_models():
    # R-COST: без reasoning_effort="minimal" reasoning-модель может потратить
    # весь max_completion_tokens на скрытые reasoning-токены и вернуть пустую
    # строку — это не гипотеза, поймано реальным вызовом API (см. память
    # проекта). Тест защищает, что параметр реально уходит в запрос.
    llm, fake = _llm_with_fake_client("gpt-5-mini")
    llm.chat([{"role": "user", "content": "привет"}])
    assert fake.received_kwargs["reasoning_effort"] == "minimal"
    assert "max_completion_tokens" in fake.received_kwargs
    assert "max_tokens" not in fake.received_kwargs


def test_reasoning_effort_not_sent_for_non_reasoning_models():
    llm, fake = _llm_with_fake_client("gpt-4.1-mini")
    llm.chat([{"role": "user", "content": "привет"}])
    assert "reasoning_effort" not in fake.received_kwargs
    assert "max_tokens" in fake.received_kwargs


def test_empty_reasoning_effort_is_not_sent():
    llm, fake = _llm_with_fake_client("gpt-5-mini", reasoning_effort="")
    llm.chat([{"role": "user", "content": "привет"}])
    assert "reasoning_effort" not in fake.received_kwargs


def test_empty_response_retries_with_bigger_budget_for_reasoning_model():
    # R-COST: reasoning_effort="minimal" снижает шанс пустого ответа, но не
    # гарантирует его при большом контексте — поймано реальными вызовами API
    # (нестабильно от запуска к запуску). Вместо большого бюджета на КАЖДЫЙ
    # вызов — платим за повторный вызов только когда первый вернулся пустым.
    llm, fake = _llm_with_fake_client("gpt-5-mini", contents=("", "настоящий ответ"))
    answer = llm.chat([{"role": "user", "content": "вопрос"}], max_tokens=500)
    assert answer == "настоящий ответ"
    assert len(fake.calls) == 2
    assert fake.calls[0]["max_completion_tokens"] == 500
    assert fake.calls[1]["max_completion_tokens"] == 1500  # 500 * _EMPTY_RESPONSE_RETRY_MULTIPLIER


def test_empty_response_retry_capped_at_ceiling():
    llm, fake = _llm_with_fake_client("gpt-5-mini", contents=("", "ответ"))
    llm.chat([{"role": "user", "content": "вопрос"}], max_tokens=900)
    assert fake.calls[1]["max_completion_tokens"] == 2000  # min(900*3, 2000) — потолок


def test_empty_response_no_retry_for_non_reasoning_model():
    llm, fake = _llm_with_fake_client("gpt-4.1-mini", contents=("", "не должно понадобиться"))
    answer = llm.chat([{"role": "user", "content": "вопрос"}], max_tokens=500)
    assert answer == ""
    assert len(fake.calls) == 1


def test_non_empty_first_response_does_not_retry():
    llm, fake = _llm_with_fake_client("gpt-5-mini", contents=("уже готовый ответ",))
    answer = llm.chat([{"role": "user", "content": "вопрос"}])
    assert answer == "уже готовый ответ"
    assert len(fake.calls) == 1
