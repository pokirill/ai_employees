from __future__ import annotations

from shared.sprint_digest import build_sprint_digest
from shared.task_store import Task


def _task(**overrides) -> Task:
    defaults = dict(
        id=1,
        title="Задача",
        status="open",
        claimed_by=None,
        created_by="Аня",
        created_at="2026-07-12T10:00:00+00:00",
        completed_at=None,
    )
    defaults.update(overrides)
    return Task(**defaults)


def test_no_activity_at_all_returns_none():
    assert build_sprint_digest([], [], [], period_label="07.07-13.07") is None


def test_shows_counts_and_titles_per_bucket():
    done = [_task(id=1, title="Сделано А"), _task(id=2, title="Сделано Б")]
    cancelled = [_task(id=3, title="Отменено В", status="cancelled")]
    still_open = [_task(id=4, title="Открыто Г")]

    digest = build_sprint_digest(done, cancelled, still_open, period_label="07.07-13.07")

    assert "07.07-13.07" in digest
    assert "✅ Сделали (2): «Сделано А», «Сделано Б»" in digest
    assert "❌ Отменили (1): «Отменено В»" in digest
    assert "➡️ Перенесли (1): «Открыто Г»" in digest


def test_empty_bucket_shows_dash():
    digest = build_sprint_digest([_task(title="Что-то")], [], [], period_label="07.07-13.07")

    assert "❌ Отменили: —" in digest
    assert "➡️ Перенесли: —" in digest


def test_still_open_testing_task_gets_suffix():
    still_open = [_task(id=5, title="В процессе", status="testing")]

    digest = build_sprint_digest([], [], still_open, period_label="07.07-13.07")

    assert "«В процессе» [тестируется]" in digest


def test_escapes_html_in_titles():
    done = [_task(title="<script>alert(1)</script>")]

    digest = build_sprint_digest(done, [], [], period_label="07.07-13.07")

    assert "<script>" not in digest
    assert "&lt;script&gt;" in digest
