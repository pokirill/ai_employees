from __future__ import annotations

from team_bot.custdev import CustDevSession, build_summary, continue_interview, start_interview


class _FakeLLM:
    def __init__(self, responses):
        self.calls = []
        self._responses = list(responses)

    def chat(self, messages, **kwargs):
        self.calls.append(messages)
        if len(self._responses) > 1:
            return self._responses.pop(0)
        return self._responses[0]


def test_start_interview_seeds_history_with_opening_question():
    llm = _FakeLLM(["Расскажи о недавней ситуации с деньгами?"])
    session = start_interview(llm, chat_id=42, admin_username="fullbyte9", target_username="ivan")
    assert session.chat_id == 42
    assert session.admin_username == "fullbyte9"
    assert session.target_username == "ivan"
    assert session.history == [{"role": "assistant", "content": "Расскажи о недавней ситуации с деньгами?"}]
    assert session.turns == 0


def test_continue_interview_appends_history_and_continues_when_not_done():
    llm = _FakeLLM(["А что случилось дальше?"])
    session = CustDevSession(chat_id=1, admin_username="a", target_username="ivan")
    reply, done = continue_interview(llm, session, "Хотел накопить на телефон, но не получилось.")
    assert reply == "А что случилось дальше?"
    assert done is False
    assert session.turns == 1
    assert session.history[0] == {"role": "user", "content": "Хотел накопить на телефон, но не получилось."}
    assert session.history[-1] == {"role": "assistant", "content": "А что случилось дальше?"}


def test_continue_interview_detects_gotovo_and_strips_it():
    llm = _FakeLLM(["Спасибо за ответы!\nГОТОВО"])
    session = CustDevSession(chat_id=1, admin_username="a", target_username="ivan")
    reply, done = continue_interview(llm, session, "Ну вот и всё, собственно.")
    assert done is True
    assert "ГОТОВО" not in reply
    assert "Спасибо за ответы!" in reply


def test_continue_interview_force_wraps_up_after_max_turns():
    # 8 предыдущих ходов уже накоплено — 8-й continue_interview должен
    # принудительно завершить интервью, не дожидаясь слова ГОТОВО от модели.
    llm = _FakeLLM(["Спасибо, было полезно!"])
    session = CustDevSession(chat_id=1, admin_username="a", target_username="ivan", turns=7)
    reply, done = continue_interview(llm, session, "Ещё один ответ.")
    assert done is True
    assert reply == "Спасибо, было полезно!"


def test_build_summary_without_any_reply_returns_honest_message_and_skips_llm():
    llm = _FakeLLM(["не должно вызываться"])
    session = CustDevSession(chat_id=1, admin_username="fullbyte9", target_username="ivan")
    session.history.append({"role": "assistant", "content": "Открывающий вопрос"})
    summary = build_summary(llm, session)
    assert "не состоялось" in summary
    assert "ivan" in summary
    assert llm.calls == []


def test_build_summary_includes_target_and_admin_usernames():
    llm = _FakeLLM(["<b>Триггер:</b> тест"])
    session = CustDevSession(chat_id=1, admin_username="fullbyte9", target_username="ivan")
    session.history.append({"role": "assistant", "content": "Вопрос"})
    session.history.append({"role": "user", "content": "Ответ собеседника"})
    summary = build_summary(llm, session)
    assert "@ivan" in summary
    assert "@fullbyte9" in summary
    assert "Триггер" in summary
