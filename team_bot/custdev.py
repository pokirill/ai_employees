from __future__ import annotations

import logging
from dataclasses import dataclass, field

from shared.llm_client import LLMClient

logger = logging.getLogger("team_bot.custdev")

# Страховка от зависшего интервью, если модель забудет написать ГОТОВО
# (см. _INTERVIEWER_SYSTEM_PROMPT, п.6) — не бесконечный цикл вопросов.
_MAX_TURNS = 8

# R-AJTBD: та же логика "триггер → Core Job → барьеры", что в
# Docs/skills/AJTBD.md (используется для постов в канал) — здесь применена к
# ЖИВОМУ интервью, а не к тексту поста. Явно просили живой диалог "вопросы,
# которые будут помогать по прилке", а не фиксированный скрипт.
_INTERVIEWER_SYSTEM_PROMPT = (
    "Ты проводишь customer development интервью для приложения «Кубышка» "
    "(планирование личного бюджета, накопления на цели). Методология — "
    "Jobs To Be Done: тебя интересует не мнение о фиче, а РЕАЛЬНАЯ недавняя "
    "ситуация из жизни собеседника.\n\n"
    "Правила:\n"
    "1. Начинай с открытого вопроса про недавнюю ситуацию, связанную с "
    "деньгами/бюджетом/накоплениями (НЕ про приложение конкретно) — дай "
    "собеседнику рассказать своими словами.\n"
    "2. Копай вглубь: какая была ситуация-триггер, что он пытался сделать "
    "(Core Job), что мешало (барьеры), чем закончилось, что почувствовал.\n"
    "3. НЕ задавай наводящих вопросов («вам не хватает функции X?») — дай "
    "человеку самому назвать проблему своими словами.\n"
    "4. Один вопрос за раз, коротко, разговорным тоном — это чат, не анкета.\n"
    "5. Если ответ короткий/общий — уточни конкретику («а что именно "
    "произошло», «на каком шаге застряли»), не переходи к следующей теме "
    "сразу.\n"
    "6. Когда картина уже ясна (обычно после 4-6 вопросов) или собеседник "
    "явно исчерпал тему — поблагодари и заверши интервью, добавив слово "
    "ГОТОВО отдельной строкой в конце сообщения."
)

_SUMMARY_SYSTEM_PROMPT = (
    "Ты анализируешь транскрипт customer development интервью (методология "
    "Jobs To Be Done) для команды продукта «Кубышка». Составь краткую сводку "
    "в Telegram HTML (теги <b>, никакого markdown):\n"
    "<b>Триггер:</b> в какой ситуации оказался собеседник\n"
    "<b>Core Job:</b> что он на самом деле пытался сделать\n"
    "<b>Барьеры:</b> что мешало/раздражало\n"
    "<b>Цитаты:</b> 1-2 дословные яркие фразы собеседника\n"
    "<b>Идея для продукта:</b> одна строка, если из интервью следует "
    "конкретная гипотеза — иначе пропусти пункт целиком.\n"
    "Коротко, без воды. Если интервью слишком короткое/пустое для выводов — "
    "честно напиши это, не выдумывай."
)


@dataclass
class CustDevSession:
    chat_id: int
    admin_username: str
    target_username: str
    history: list[dict[str, str]] = field(default_factory=list)
    turns: int = 0


def start_interview(llm: LLMClient, chat_id: int, admin_username: str, target_username: str) -> CustDevSession:
    """Создаёт сессию и сразу задаёт первый вопрос (без истории, только
    системный промпт) — открывающий вопрос уже лежит в session.history."""
    messages = [
        {"role": "system", "content": _INTERVIEWER_SYSTEM_PROMPT},
        {"role": "user", "content": "Начни интервью — задай первый вопрос."},
    ]
    opening = llm.chat(messages, max_tokens=250, temperature=0.8)
    session = CustDevSession(chat_id=chat_id, admin_username=admin_username, target_username=target_username)
    session.history.append({"role": "assistant", "content": opening})
    return session


def continue_interview(llm: LLMClient, session: CustDevSession, reply_text: str) -> tuple[str, bool]:
    """Возвращает (текст_ответа_бота, интервью_завершено). Завершение решает
    сама модель (слово ГОТОВО), session.turns — только страховка от
    зависшего интервью."""
    session.history.append({"role": "user", "content": reply_text})
    session.turns += 1

    if session.turns >= _MAX_TURNS:
        closing = _wrap_up(llm, session)
        session.history.append({"role": "assistant", "content": closing})
        return closing, True

    messages = [{"role": "system", "content": _INTERVIEWER_SYSTEM_PROMPT}] + session.history
    answer = llm.chat(messages, max_tokens=300, temperature=0.8)
    is_done = "ГОТОВО" in answer.upper()
    if is_done:
        answer = answer.replace("ГОТОВО", "").replace("готово", "").strip()
    session.history.append({"role": "assistant", "content": answer})
    return answer, is_done


def _wrap_up(llm: LLMClient, session: CustDevSession) -> str:
    messages = [
        {"role": "system", "content": _INTERVIEWER_SYSTEM_PROMPT},
        *session.history,
        {"role": "user", "content": "Заверши интервью коротким благодарственным сообщением — больше вопросов не задавай."},
    ]
    return llm.chat(messages, max_tokens=150, temperature=0.7)


def build_summary(llm: LLMClient, session: CustDevSession) -> str:
    user_turns = [m for m in session.history if m["role"] == "user"]
    if not user_turns:
        return f"Интервью с @{session.target_username} не состоялось — собеседник не ответил ни разу."
    transcript = "\n".join(
        f"{'Интервьюер' if m['role'] == 'assistant' else 'Собеседник'}: {m['content']}" for m in session.history
    )
    messages = [
        {"role": "system", "content": _SUMMARY_SYSTEM_PROMPT},
        {"role": "user", "content": f"Транскрипт интервью с @{session.target_username}:\n\n{transcript}"},
    ]
    summary = llm.chat(messages, max_tokens=500, temperature=0.4)
    return f"🎤 <b>CustDev с @{session.target_username}</b> (запросил @{session.admin_username})\n\n{summary}"
