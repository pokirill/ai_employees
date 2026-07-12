from shared.llm_client import _is_reasoning_model


def test_reasoning_models_detected():
    assert _is_reasoning_model("gpt-5-mini")
    assert _is_reasoning_model("gpt-5")
    assert _is_reasoning_model("o1-preview")
    assert _is_reasoning_model("o3-mini")


def test_non_reasoning_models_not_flagged():
    assert not _is_reasoning_model("gpt-4.1-mini")
    assert not _is_reasoning_model("gpt-4o")
