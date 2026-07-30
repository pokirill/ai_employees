from __future__ import annotations

from datetime import date

from shared.metrics_digest import build_metrics_digest


def _dashboard(**overrides) -> dict:
    defaults = dict(
        data_quality={"known_people": 135},
        product={
            "active_users": {"latest": {"dau": 19, "wau": 57, "mau": 129}},
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


def test_includes_dau_wau_mau():
    digest = build_metrics_digest(_dashboard(), [], today=date(2026, 7, 30))
    assert "DAU 19" in digest
    assert "WAU 57" in digest
    assert "MAU 129" in digest


def test_counts_new_today_from_persons_by_first_seen_date():
    persons = [
        _person(person_id="a", first_seen_at="2026-07-30 09:00 MSK"),
        _person(person_id="b", first_seen_at="2026-07-30 11:00 MSK"),
        _person(person_id="c", first_seen_at="2026-07-29 09:00 MSK"),
    ]
    digest = build_metrics_digest(_dashboard(), persons, today=date(2026, 7, 30))
    assert "+2 новых сегодня" in digest


def test_counts_active_24h_from_persons():
    persons = [
        _person(person_id="a", active_24h=True),
        _person(person_id="b", active_24h=True),
        _person(person_id="c", active_24h=False),
    ]
    digest = build_metrics_digest(_dashboard(), persons, today=date(2026, 7, 30))
    assert "2 активны за 24ч" in digest


def test_retention_checkpoints_with_dash_are_omitted():
    digest = build_metrics_digest(_dashboard(), [], today=date(2026, 7, 30))
    assert "D1 7.8%" in digest
    assert "D30" not in digest.split("Retention:")[1].split("\n")[0]


def test_only_warn_risks_are_shown():
    digest = build_metrics_digest(_dashboard(), [], today=date(2026, 7, 30))
    assert "Не все установки склеены" in digest
    assert "Всё ок" not in digest
    assert "Стоит поднажать по этим вещам:" in digest


def test_motivational_line_uses_real_ok_ratio():
    digest = build_metrics_digest(_dashboard(), [], today=date(2026, 7, 30))
    assert "Уже 50% ключевых метрик (1 из 2) в зелёной зоне" in digest


def test_target_with_angle_bracket_is_escaped_for_telegram_html():
    dashboard = _dashboard(
        product={
            "active_users": {"latest": {"dau": 19, "wau": 57, "mau": 129}},
            "metrics": [
                {"label": "Median TTV", "value": "7.6 мин", "target": "< 3 мин", "status": "warn"},
            ],
            "retention": {"checkpoints": []},
            "paywall": {},
            "launch_risks": [{"risk": "A & B < C", "metric": "x", "status": "warn"}],
        }
    )
    digest = build_metrics_digest(dashboard, [], today=date(2026, 7, 30))
    assert "&lt; 3 мин" in digest
    assert "< 3 мин" not in digest
    assert "A &amp; B &lt; C" in digest


def test_metric_status_icons():
    digest = build_metrics_digest(_dashboard(), [], today=date(2026, 7, 30))
    assert "✅ Конверсия онбординга" in digest
    assert "⚠️ Активация 48ч" in digest


def test_empty_metrics_and_persons_does_not_crash():
    digest = build_metrics_digest(_dashboard(product={"active_users": {"latest": {}}}), [], today=date(2026, 7, 30))
    assert "DAU —" in digest
