from __future__ import annotations

from custdev_bot.triggers import mentions_money_tracking


def test_matches_budget_keyword():
    assert mentions_money_tracking("я вообще не веду бюджет никак")


def test_matches_spending_keyword():
    assert mentions_money_tracking("не понимаю, куда уходят деньги каждый месяц")


def test_matches_case_insensitive():
    assert mentions_money_tracking("ТРАЧУ слишком много на еду")


def test_no_match_for_unrelated_text():
    assert not mentions_money_tracking("кто-нибудь смотрел новый фильм?")


def test_no_match_for_empty_text():
    assert not mentions_money_tracking("")
