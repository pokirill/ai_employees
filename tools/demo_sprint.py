#!/usr/bin/env python3
"""Демонстрация рабочего пространства команды без единого ключа и без сети.

Запусти и посмотри, что увидит команда:

    python3 tools/demo_sprint.py

Работает на временной базе в /tmp, ничего не трогает и никуда не пишет.
Нужен, чтобы решение можно было оценить до того, как заводить токены Miro и
пароль приложения Apple: сначала смотрим, потом подключаем.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from html import unescape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared import epics, sprint_planner, sprint_report  # noqa: E402
from shared.miro_client import COLUMNS  # noqa: E402
from shared.sprints import LEVEL_BUSY, LEVEL_FREE, LEVEL_NORMAL, SprintStore  # noqa: E402
from shared.sync_engine import SyncState, sync_miro  # noqa: E402
from shared.task_store import TaskStore  # noqa: E402

# Задачи взяты из настоящих обсуждений команды — чтобы демонстрация не
# выглядела набором «задача 1, задача 2».
SAMPLE = [
    ("Починить дубли трат при импорте скриншота", 0, 3),
    ("Выпустить сертификат Let's Encrypt на pay.kubysh.com", 0, 1),
    ("Экран подписки в приложении", 1, 10),
    ("Оферта: получить текст у юриста", 1, 2),
    ("Написать пост про подушку в канал", 1, 2),
    ("Кастдев: пять интервью про оплату", 1, 8),
    ("Рефакторинг расчёта дневного лимита", 2, 6),
    ("Разобрать конкурента ZenMoney", 3, 4),
    ("Уведомление в РКН", 1, 3),
    ("Настроить счётчик Метрики на лендинге", 2, 2),
]

TEAM = [
    ("Саша", 1, LEVEL_NORMAL, None, ""),
    ("Кирилл", 2, LEVEL_BUSY, None, "много встреч на этой неделе"),
    ("Ваня", 3, LEVEL_FREE, 25.0, ""),
]


class DemoBoard:
    """Miro в памяти: показываем, как разложатся карточки, без токена."""

    enabled = True

    def __init__(self) -> None:
        self.cards: dict[str, dict] = {}
        self.frames = {status: f"frame-{status}" for status, _ in COLUMNS}
        self._next = 1

    def ensure_columns(self):
        return dict(self.frames)

    def list_cards(self):
        return []

    def create_card(self, *, title, description, frame_id, index):
        item = f"card-{self._next}"
        self._next += 1
        self.cards[item] = {"title": title, "frame": frame_id}
        return item

    def update_card(self, item_id, *, title, description, frame_id):
        self.cards[item_id] = {"title": title, "frame": frame_id}


def plain(text: str) -> str:
    """HTML в читаемый текст: в терминале теги и мнемоники только мешают."""
    text = re.sub(r"<[^>]+>", "", text)
    # Без unescape апостроф в «Let's» показывался бы как &#x27;
    return unescape(text)


def head(title: str) -> None:
    print()
    print("─" * 68)
    print(f"  {title}")
    print("─" * 68)


def main() -> int:
    db = tempfile.mktemp(suffix=".db")
    tasks = TaskStore(db)
    sprints = SprintStore(db)
    state = SyncState(db)

    try:
        head("1. Импорт: задачи из Напоминаний и переписки → эпики")
        for title, priority, estimate in SAMPLE:
            task = tasks.add_task(title, created_by="импорт", epic=epics.classify(title), origin="import")
            tasks.set_priority(task.id, priority)
            tasks.set_estimate(task.id, estimate)

        by_epic: dict[str, list] = {}
        for task in tasks.list_backlog():
            by_epic.setdefault(task.epic or epics.UNSORTED, []).append(task)
        for code, items in sorted(by_epic.items(), key=lambda item: -len(item[1])):
            print(f"\n  {epics.label(code)} — {len(items)}")
            for task in items:
                print(f"      P{task.priority}  {task.title}")
        print("\n  Эпики расставлены по ключевым словам — без модели и без сети.")

        head("2. Начало спринта: команда говорит, кто насколько занят")
        sprint = sprints.start(goal="Запустить оплату на сайте")
        print(f"\n  🚀 {sprint.title}")
        print(f"  Цель: {sprint.goal}")
        print(f"  Период: {sprint.period_label}, дней осталось: {sprint.days_left()}")
        print("\n  [🔴 Занят]  [🟡 Обычно]  [🟢 Есть время]   ← кнопки в чате\n")
        for person, user_id, level, hours, note in TEAM:
            capacity = sprints.declare_capacity(
                sprint.id, person=person, person_user_id=user_id, level=level, hours=hours, note=note
            )
            suffix = f" ({capacity.note})" if capacity.note else ""
            print(f"      {person}: {capacity.level_title}, ориентир {capacity.hours:g} ч{suffix}")

        head("3. Предложение: как разложить бэклог")
        proposal = sprint_planner.propose(tasks.list_backlog(), sprints.capacities(sprint.id))
        print()
        print(plain(sprint_planner.render(proposal, sprint_title=sprint.title)))

        head("4. Канбан в Miro: карточки по колонкам")
        for task in tasks.list_backlog()[:6]:
            tasks.set_sprint(task.id, sprint.id)
        board = DemoBoard()
        sync_miro(tasks, state, board, sprint_id=sprint.id)
        columns: dict[str, list[str]] = {title: [] for _, title in COLUMNS}
        frame_titles = {f"frame-{status}": title for status, title in COLUMNS}
        for card in board.cards.values():
            columns[frame_titles[card["frame"]]].append(card["title"])
        for title, items in columns.items():
            print(f"\n  ┌─ {title} ({len(items)})")
            for item in items:
                print(f"  │  {item[:56]}")
        print("\n  Двинул карточку мышкой — статус меняется в боте, владельцу приходит личное сообщение.")

        head("5. Работа идёт: часть задач закрыта")
        mine = tasks.list_by_sprint(sprint.id)[:4]
        for task in mine:
            tasks.claim_task(task.id, "Саша", 1)
        tasks.complete_task(mine[0].id)
        tasks.complete_task(mine[1].id)
        tasks.complete_task(mine[2].id)
        print(f"\n  Саша взял {len(mine)}, закрыл 3.")

        head("6. Закончились задачи: /more")
        print()
        print(plain(sprint_report.suggest_more(tasks.list_backlog(), person="Саша")))
        extra = tasks.list_backlog()[0]
        tasks.claim_task(extra.id, "Саша", 1)
        print()
        print("  В общий чат при этом уходит:")
        print("  " + plain(sprint_report.announce_pickup("Саша", extra)))

        head("7. Конец спринта: личные итоги (в личку)")
        results = sprint_report.collect(tasks.list_by_sprint(sprint.id), sprints.capacities(sprint.id))
        for result in results:
            print()
            print(plain(sprint_report.render_personal(result, sprint)))

        head("8. Конец спринта: командные итоги (в чат)")
        unassigned = [t for t in tasks.list_by_sprint(sprint.id) if not t.claimed_by and t.status != "done"]
        print()
        print(plain(sprint_report.render_team(results, sprint, unassigned=unassigned)))

        print()
        print("─" * 68)
        print("  Всё это работает уже сейчас. Для Miro нужен токен, для")
        print("  Напоминаний — пароль приложения Apple. Без них остальное не ломается.")
        print("─" * 68)
        return 0
    finally:
        if os.path.exists(db):
            os.remove(db)


if __name__ == "__main__":
    raise SystemExit(main())
