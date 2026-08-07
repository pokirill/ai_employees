from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from shared.metrics_digest import (
    MOSCOW_TZ,
    build_glossary,
    build_metrics_digest,
    last_complete_day,
)

REPORT_DAY = date(2026, 7, 30)


def _dashboard(**overrides) -> dict:
    defaults = dict(
        data_quality={"known_people": 135},
        product={
            "active_users": {
                # series — источник по КОНКРЕТНЫМ дням; latest всегда «сегодня»
                # по МСК и в полночь почти пустой, поэтому дайджест обязан
                # выбирать день из series, а не брать latest.
                "series": [
                    {"date": "2026-07-29", "dau": 12, "wau": 40, "mau": 100},
                    {"date": "2026-07-30", "dau": 19, "wau": 57, "mau": 129},
                    {"date": "2026-07-31", "dau": 0, "wau": 57, "mau": 129},
                ],
                "latest": {"dau": 0, "wau": 57, "mau": 129},
            },
            "metrics": [
                {"label": "Конверсия онбординга", "value": "24.0%", "target": ">= 15%", "status": "ok"},
                {"label": "Активация 48ч", "value": "2.6%", "target": ">= 40%", "status": "warn"},
            ],
            "retention": {
                "checkpoints": [
                    {"day": "D1", "rate": "7.8%"},
                    {"day": "D7", "rate": "2.6%"},
                    {"day": "D14", "rate": "10.8%"},
                    {"day": "D30", "rate": "—"},
                ]
            },
            "paywall": {"shown": 26, "trial_accepted": 24, "trial_conversion": "92.3%", "purchase_completed": 0},
            "launch_risks": [
                {"risk": "Не все установки склеены", "metric": "135/135", "status": "warn"},
                {"risk": "Всё ок", "metric": "0 gaps", "status": "ok"},
            ],
        },
    )
    defaults.update(overrides)
    return defaults


def _person(**overrides) -> dict:
    defaults = dict(person_id="p1", first_seen_at="2026-07-29 10:00 MSK", active_24h=False)
    defaults.update(overrides)
    return defaults


# MARK: - Отчёт за нужные сутки (главная правка)


def test_activity_is_taken_for_the_report_day_not_latest():
    """Ключевое: в полночь latest = новые сутки с нулём. Берём день из series."""
    digest = build_metrics_digest(_dashboard(), [], report_date=REPORT_DAY)

    assert "Заходили в приложение: 19 чел." in digest
    assert "за неделю 57" in digest
    assert "за месяц 129" in digest
    # Ноль из latest не должен просочиться.
    assert "Заходили в приложение: 0 чел." not in digest


def test_report_date_is_in_the_header():
    digest = build_metrics_digest(_dashboard(), [], report_date=REPORT_DAY)
    assert "30.07.2026" in digest


def test_missing_day_in_series_falls_back_and_says_so():
    """Молча подсунуть чужой день нельзя — иначе числа врут без предупреждения."""
    digest = build_metrics_digest(_dashboard(), [], report_date=date(2026, 1, 1))

    assert "последний доступный день" in digest


def test_partial_day_is_marked_in_header():
    """Ручной /metricsnow посреди суток не должен читаться как итог дня."""
    digest = build_metrics_digest(
        _dashboard(), [], report_date=REPORT_DAY, partial_day=True
    )
    assert "неполные сутки" in digest
    assert "день ещё идёт" in digest


def test_last_complete_day_returns_yesterday_at_midnight_msk():
    midnight = datetime(2026, 7, 31, 0, 0, tzinfo=MOSCOW_TZ)
    assert last_complete_day(midnight) == date(2026, 7, 30)


def test_last_complete_day_handles_other_timezones():
    """Машина может жить в UTC — считать надо всё равно по Москве."""
    # 2026-07-30 22:00 UTC = 2026-07-31 01:00 МСК → закончились сутки 30-го.
    utc_moment = datetime(2026, 7, 30, 22, 0, tzinfo=ZoneInfo("UTC"))
    assert last_complete_day(utc_moment) == date(2026, 7, 30)


def test_new_people_counted_for_report_day_not_calendar_today():
    persons = [
        _person(person_id="a", first_seen_at="2026-07-30 09:00 MSK"),
        _person(person_id="b", first_seen_at="2026-07-30 11:00 MSK"),
        _person(person_id="c", first_seen_at="2026-07-29 09:00 MSK"),
    ]
    digest = build_metrics_digest(_dashboard(), persons, report_date=REPORT_DAY)
    assert "+2 впервые за этот день" in digest


# MARK: - Расхождение DAU vs «активные за 24ч»


def test_rolling_24h_number_is_not_shown_at_all():
    """Корень путаницы основателя: два похожих числа рядом.

    «Активные за 24ч» из persons.json — скользящее окно по UTC и ЛЮБОЕ событие,
    а DAU — календарные сутки МСК и только осмысленные действия. Показывать их
    рядом означает раз в неделю объяснять, почему они не сходятся. Убрано.
    """
    persons = [
        _person(person_id="a", active_24h=True),
        _person(person_id="b", active_24h=True),
        _person(person_id="c", active_24h=False),
    ]
    digest = build_metrics_digest(_dashboard(), persons, report_date=REPORT_DAY)

    assert "активны за 24" not in digest
    assert "2 активны" not in digest


def test_glossary_explains_the_dau_vs_active_difference():
    glossary = build_glossary()
    assert "не совпадает" in glossary
    assert "скользящее окно" in glossary
    assert "не ошибка расчёта" in glossary


# MARK: - Язык без жаргона


def test_paywall_wording_is_plain_russian():
    """«Что же такое пейвол?» — вопрос основателя. Слова быть не должно."""
    digest = build_metrics_digest(_dashboard(), [], report_date=REPORT_DAY)

    assert "paywall" not in digest.lower()
    assert "пейвол" not in digest.lower()
    assert "Оплата: экран подписки видели 26 чел." in digest
    assert "пробный период включили 24" in digest
    assert "оплатили 0" in digest


def test_digest_points_to_glossary():
    """Команда латиницей: кириллическую Telegram не примет в меню BotFather,
    и остальные команды бота тоже латиницей (/metricsnow, /board)."""
    digest = build_metrics_digest(_dashboard(), [], report_date=REPORT_DAY)
    assert "/terms" in digest


def test_glossary_covers_every_term_used_in_digest():
    glossary = build_glossary().lower()
    for term in ["экран подписки", "dau", "retention", "активация 48ч", "конверсия онбординга"]:
        assert term.lower() in glossary, f"нет расшифровки: {term}"


def test_retention_line_is_human_readable():
    digest = build_metrics_digest(_dashboard(), [], report_date=REPORT_DAY)
    assert "Возвращаются в приложение: D1 7.8%" in digest


# MARK: - Регрессии прежнего поведения


def test_retention_checkpoints_with_dash_are_omitted():
    digest = build_metrics_digest(_dashboard(), [], report_date=REPORT_DAY)
    line = digest.split("Возвращаются в приложение:")[1].split("\n")[0]
    assert "D1 7.8%" in line
    assert "D30" not in line


def test_only_warn_risks_are_shown():
    digest = build_metrics_digest(_dashboard(), [], report_date=REPORT_DAY)
    assert "Не все установки склеены" in digest
    assert "Всё ок" not in digest
    assert "Стоит поднажать по этим вещам:" in digest


def test_motivational_line_uses_real_ok_ratio():
    digest = build_metrics_digest(_dashboard(), [], report_date=REPORT_DAY)
    assert "Уже 50% ключевых метрик (1 из 2) в зелёной зоне" in digest


def test_target_with_angle_bracket_is_escaped_for_telegram_html():
    dashboard = _dashboard(
        product={
            "active_users": {
                "series": [{"date": "2026-07-30", "dau": 19, "wau": 57, "mau": 129}],
                "latest": {"dau": 19, "wau": 57, "mau": 129},
            },
            "metrics": [
                {"label": "Median TTV", "value": "7.6 мин", "target": "< 3 мин", "status": "warn"},
            ],
            "retention": {"checkpoints": []},
            "paywall": {},
            "launch_risks": [{"risk": "A & B < C", "metric": "x", "status": "warn"}],
        }
    )
    digest = build_metrics_digest(dashboard, [], report_date=REPORT_DAY)
    assert "&lt; 3 мин" in digest
    assert "< 3 мин" not in digest
    assert "A &amp; B &lt; C" in digest


def test_metric_status_icons():
    digest = build_metrics_digest(_dashboard(), [], report_date=REPORT_DAY)
    assert "✅ Конверсия онбординга" in digest
    assert "⚠️ Активация 48ч" in digest


def test_empty_dashboard_does_not_crash():
    digest = build_metrics_digest({}, [], report_date=REPORT_DAY)
    assert "Кубыши, привет" in digest
    assert "/terms" in digest


def test_report_date_defaults_to_last_complete_day():
    """Без явной даты дайджест обязан взять завершившиеся сутки, а не сегодня."""
    digest = build_metrics_digest(_dashboard(), [])
    expected = last_complete_day().strftime("%d.%m.%Y")
    assert expected in digest


def test_timedelta_import_is_used_for_day_math():
    """Страховка от случайного удаления импорта при рефакторинге."""
    assert last_complete_day(datetime(2026, 3, 1, 0, 30, tzinfo=MOSCOW_TZ)) == date(2026, 2, 28)
    assert timedelta(days=1).days == 1
