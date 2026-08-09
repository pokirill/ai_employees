"""Расписание дайджеста метрик: переменная окружения не должна тихо возвращать 21:00.

Реальный инцидент: код перевели на полночь, изменения запушили, но сводка снова
пришла в 21:00 МСК. Причина была не в коде — в рабочем `.env` осталась строка
`TEAM_METRICS_HOUR=21` с прежних времён, а переменная окружения сильнее дефолта.
Поэтому переменную переименовали: стухшее значение перестаёт применяться, и
обновления кода достаточно.
"""

from __future__ import annotations

import importlib

import pytest


def _fresh_config(monkeypatch, env: dict[str, str | None]):
    """Пересоздаёт конфиг с нужным окружением.

    Конфиг — frozen dataclass с default_factory, значения читаются в момент
    создания экземпляра, поэтому достаточно импортировать класс и построить
    новый объект под изменённым окружением.
    """
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    # Обязательные переменные, без которых конфиг не построится.
    monkeypatch.setenv("TEAM_BOT_TOKEN", "test-token")
    module = importlib.import_module("shared.config")
    importlib.reload(module)
    return module.TeamBotConfig()


def test_default_is_midnight_when_nothing_set(monkeypatch):
    config = _fresh_config(monkeypatch, {"TEAM_METRICS_HOUR_MSK": None, "TEAM_METRICS_HOUR": None})
    assert config.metrics_hour == 0


def test_legacy_variable_no_longer_changes_the_schedule(monkeypatch):
    """Главный смысл переименования: старое 21 больше не применяется.

    Без этого на сервере пришлось бы руками править .env, а до тех пор сводка
    продолжала бы приходить в 21:00 после любого обновления кода.
    """
    config = _fresh_config(
        monkeypatch, {"TEAM_METRICS_HOUR": "21", "TEAM_METRICS_HOUR_MSK": None}
    )

    assert config.metrics_hour == 0, "старая переменная не должна влиять на расписание"


def test_legacy_variable_is_remembered_for_the_warning(monkeypatch):
    """Игнорировать молча нельзя — человек должен узнать из лога."""
    config = _fresh_config(
        monkeypatch, {"TEAM_METRICS_HOUR": "21", "TEAM_METRICS_HOUR_MSK": None}
    )

    assert config.legacy_metrics_hour_env == "21"


def test_no_warning_when_legacy_variable_is_absent(monkeypatch):
    config = _fresh_config(monkeypatch, {"TEAM_METRICS_HOUR": None, "TEAM_METRICS_HOUR_MSK": None})
    assert config.legacy_metrics_hour_env == ""


def test_new_variable_still_wins_when_set_deliberately(monkeypatch):
    """Осознанная настройка должна работать — мы убрали ловушку, а не гибкость."""
    config = _fresh_config(
        monkeypatch, {"TEAM_METRICS_HOUR_MSK": "9", "TEAM_METRICS_HOUR": None}
    )
    assert config.metrics_hour == 9


def test_new_variable_wins_over_legacy_when_both_present(monkeypatch):
    config = _fresh_config(
        monkeypatch, {"TEAM_METRICS_HOUR_MSK": "3", "TEAM_METRICS_HOUR": "21"}
    )
    assert config.metrics_hour == 3
    assert config.legacy_metrics_hour_env == "21"


@pytest.mark.parametrize("hour", ["0", "9", "23"])
def test_valid_hours_are_accepted(monkeypatch, hour):
    config = _fresh_config(
        monkeypatch, {"TEAM_METRICS_HOUR_MSK": hour, "TEAM_METRICS_HOUR": None}
    )
    assert config.metrics_hour == int(hour)
