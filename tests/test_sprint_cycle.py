"""Полный цикл спринта: бэклог → план → работа → итоги (TASK-SYS-1).

Проверяем сценарий целиком, а не отдельные функции: почти все ошибки в такой
системе живут не внутри модулей, а на стыках — задача попала в спринт, но
осталась в бэклоге; спринт закрылся, а незакрытые задачи исчезли вместе с ним.
Модульный тест такого не ловит.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared import epics, sprint_planner, sprint_report  # noqa: E402
from shared.sprints import LEVEL_BUSY, LEVEL_FREE, LEVEL_NORMAL, SprintStore  # noqa: E402
from shared.task_store import TaskStore  # noqa: E402

BACKLOG = [
    ("Починить дубли трат при импорте", 0, 3),
    ("Написать пост про подушку в канал", 1, 2),
    ("Выпустить сертификат на поддомен", 0, 1),
    ("Рефакторинг расчёта дневного лимита", 2, 6),
    ("Кастдев: пять интервью по оплате", 1, 8),
    ("Оферта: получить текст у юриста", 1, 2),
]


def _fill(store: TaskStore) -> None:
    for title, priority, estimate in BACKLOG:
        task = store.add_task(title, created_by="импорт", epic=epics.classify(title), origin="import")
        store.set_priority(task.id, priority)
        store.set_estimate(task.id, estimate)


def _setup():
    db = tempfile.mktemp(suffix=".db")
    tasks = TaskStore(db)
    sprints = SprintStore(db)
    _fill(tasks)
    return db, tasks, sprints


def test_epics_are_recognised_without_llm():
    """Классификация по правилам обязана работать без сети и без ключа."""
    db, tasks, _ = _setup()
    try:
        by_title = {t.title: t.epic for t in tasks.list_backlog()}
        assert by_title["Починить дубли трат при импорте"] == "bugs"
        assert by_title["Выпустить сертификат на поддомен"] == "infra"
        assert by_title["Написать пост про подушку в канал"] == "marketing"
        assert by_title["Оферта: получить текст у юриста"] == "legal"
    finally:
        os.remove(db)


def test_plan_respects_declared_capacity():
    """Никому не достаётся больше часов, чем он сам заявил."""
    db, tasks, sprints = _setup()
    try:
        sprint = sprints.start(goal="Запустить оплату")
        sprints.declare_capacity(sprint.id, person="Саша", person_user_id=1, level=LEVEL_NORMAL)
        sprints.declare_capacity(sprint.id, person="Кирилл", person_user_id=2, level=LEVEL_BUSY)
        proposal = sprint_planner.propose(tasks.list_backlog(), sprints.capacities(sprint.id))
        for plan in proposal.plans:
            assert plan.planned_hours <= plan.capacity_hours, plan.person
        assert proposal.planned_count > 0
    finally:
        os.remove(db)


def test_tasks_that_do_not_fit_are_reported():
    """Не влезшая задача обязана быть названа, а не потеряться молча."""
    db, tasks, sprints = _setup()
    try:
        sprint = sprints.start()
        # Один человек и очень мало часов: влезет максимум одна задача.
        sprints.declare_capacity(sprint.id, person="Саша", person_user_id=1, level=LEVEL_BUSY, hours=2)
        proposal = sprint_planner.propose(tasks.list_backlog(), sprints.capacities(sprint.id))
        assert proposal.left_out, "должно остаться что-то за бортом"
        text = sprint_planner.render(proposal, sprint_title=sprint.title)
        assert "Не поместилось" in text
    finally:
        os.remove(db)


def test_plan_is_a_proposal_not_an_assignment():
    """Планировщик НЕ проставляет исполнителей — это делает человек."""
    db, tasks, sprints = _setup()
    try:
        sprint = sprints.start()
        sprints.declare_capacity(sprint.id, person="Саша", person_user_id=1, level=LEVEL_FREE)
        sprint_planner.propose(tasks.list_backlog(), sprints.capacities(sprint.id))
        assert all(task.claimed_by is None for task in tasks.list_backlog())
    finally:
        os.remove(db)


def test_full_cycle_personal_results():
    db, tasks, sprints = _setup()
    try:
        sprint = sprints.start(goal="Запустить оплату")
        sprints.declare_capacity(sprint.id, person="Саша", person_user_id=1, level=LEVEL_NORMAL)

        # Человек берёт три задачи, две закрывает.
        taken = tasks.list_backlog()[:3]
        for task in taken:
            tasks.set_sprint(task.id, sprint.id)
            tasks.claim_task(task.id, "Саша", 1)
        tasks.complete_task(taken[0].id)
        tasks.complete_task(taken[1].id)

        results = sprint_report.collect(tasks.list_by_sprint(sprint.id), sprints.capacities(sprint.id))
        assert len(results) == 1
        result = results[0]
        assert len(result.done) == 2
        assert len(result.open_left) == 1
        assert result.completion_rate == 67

        text = sprint_report.render_personal(result, sprint)
        assert "2 из 3" in text
        assert "Переносится" in text
    finally:
        os.remove(db)


def test_full_plan_gets_praised_with_a_number():
    """Хвалим фактом, а не авансом: в тексте обязана быть цифра."""
    db, tasks, sprints = _setup()
    try:
        sprint = sprints.start()
        task = tasks.list_backlog()[0]
        tasks.set_sprint(task.id, sprint.id)
        tasks.claim_task(task.id, "Ваня", 3)
        tasks.complete_task(task.id)
        results = sprint_report.collect(tasks.list_by_sprint(sprint.id), [])
        text = sprint_report.render_personal(results[0], sprint)
        assert "1 из 1" in text
    finally:
        os.remove(db)


def test_more_tasks_suggestion_does_not_assign():
    db, tasks, sprints = _setup()
    try:
        text = sprint_report.suggest_more(tasks.list_backlog(), person="Ваня")
        assert "claim" in text
        assert all(task.claimed_by is None for task in tasks.list_backlog())
    finally:
        os.remove(db)


def test_starting_new_sprint_closes_previous():
    db, _, sprints = _setup()
    try:
        first = sprints.start(goal="Первый")
        second = sprints.start(goal="Второй")
        assert sprints.get(first.id).status == "closed"
        assert sprints.current().id == second.id
    finally:
        os.remove(db)


def test_capacity_can_be_changed():
    """«Передумал, стало свободнее» должно просто работать."""
    db, _, sprints = _setup()
    try:
        sprint = sprints.start()
        sprints.declare_capacity(sprint.id, person="Саша", person_user_id=1, level=LEVEL_BUSY)
        sprints.declare_capacity(sprint.id, person="Саша", person_user_id=1, level=LEVEL_FREE)
        capacities = sprints.capacities(sprint.id)
        assert len(capacities) == 1
        assert capacities[0].level == LEVEL_FREE
    finally:
        os.remove(db)


def test_days_left_counts_down():
    db, _, sprints = _setup()
    try:
        started = datetime.now(timezone.utc) - timedelta(days=10)
        sprint = sprints.start(now=started)
        assert 3 <= sprint.days_left() <= 4
    finally:
        os.remove(db)


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
