"""Классификация задач по эпикам (TASK-SYS-1).

Половина этих проверок — регрессии, найденные на настоящей доске из 98 задач.
Все ошибки были одного вида: ключевое слово находилось в СЕРЕДИНЕ другого
слова. «карточек» содержало «чек», «вопросы» — «опрос», «продумать» — «прод».

Неверный эпик хуже отсутствующего: он искажает картину приоритетов, на которую
потом смотрят при планировании. Поэтому здесь много проверок на то, чтобы
классификатор МОЛЧАЛ, а не угадывал.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared import epics  # noqa: E402


def test_obvious_tasks_are_classified():
    cases = {
        "Починить баг с дублями трат": "bugs",
        "Выпустить сертификат на поддомен": "infra",
        "Написать пост про подушку в канал": "marketing",
        "Оферта: получить текст у юриста": "legal",
        "Добавить оплату по СБП": "money",
        "Провести 10 кастдев интервью": "research",
        "Покрыть тестами расчёт лимита": "tech",
        "Доделать онбординг": "product",
    }
    for text, expected in cases.items():
        assert epics.classify_by_keywords(text) == expected, text


def test_keyword_inside_another_word_does_not_match():
    """Регрессии с настоящей доски: слово ищется с начала слова."""
    cases = [
        "Сгладить интерактивность карточек в соответствии с макетом",   # «чек» в «карточек»
        "Создать анимацию белых шариков вокруг телефона и после карточек",
        "Оповестить, если есть пропущенные моменты и вопросы",          # «опрос» в «вопросы»
        "Пересмотреть логику дневного бюджета: продумать математику",   # «прод» в «продумать»
        "Прислать участнице ссылку на тестовую версию приложения",      # «тест» ≠ техдолг
    ]
    for text in cases:
        assert epics.classify_by_keywords(text) is None, text


def test_word_start_still_matches_inflections():
    """Границу слова ставим слева, поэтому окончания по-прежнему ловятся."""
    assert epics.classify_by_keywords("Настроить подключение домена") == "infra"
    assert epics.classify_by_keywords("Проблема с оплатой подписки") == "money"


def test_ambiguous_task_returns_none():
    """Два эпика с равным счётом — молчим, пусть решает модель или человек."""
    # «чек» → оплата, «54-ФЗ» → юридическое: по одному совпадению у каждого.
    assert epics.classify_by_keywords("Чек по 54-ФЗ не доходит") is None


def test_classify_without_llm_never_raises():
    """Классификация не должна требовать сети: задача создаётся в любом случае."""
    assert epics.classify("Совершенно непонятная формулировка", llm=None) == epics.UNSORTED


def test_llm_answer_outside_catalog_is_ignored():
    """Модель ответила чем-то посторонним — считаем, что не определилось."""

    class Weird:
        def chat(self, *args, **kwargs):
            return "какой-то текст не из списка"

    assert epics.classify("Непонятная задача", llm=Weird()) == epics.UNSORTED


def test_llm_failure_does_not_break_creation():
    class Broken:
        def chat(self, *args, **kwargs):
            raise RuntimeError("сеть отвалилась")

    assert epics.classify("Непонятная задача", llm=Broken()) == epics.UNSORTED


def test_unknown_code_falls_back_to_unsorted():
    """В базе мог остаться код после переименования — не падаем на отображении."""
    assert epics.get("какой-то_старый_код").code == epics.UNSORTED
    assert epics.get(None).code == epics.UNSORTED


def test_all_epics_have_distinct_codes_and_emoji():
    codes = [epic.code for epic in epics.EPICS]
    emoji = [epic.emoji for epic in epics.EPICS]
    assert len(codes) == len(set(codes))
    assert len(emoji) == len(set(emoji))


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  ❌   {test.__name__}: {exc}")
    print(f"\nвсего {len(tests)}, провалов {failed}")
    sys.exit(1 if failed else 0)
