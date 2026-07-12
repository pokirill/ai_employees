from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

import caldav

_ICLOUD_CALDAV_URL = "https://caldav.icloud.com/"


class RemindersListNotFound(RuntimeError):
    pass


class TaskNotFound(RuntimeError):
    pass


@dataclass(frozen=True)
class OpenTask:
    uid: str
    title: str


class ICloudReminders:
    """Создаёт задачи в расшаренном списке Напоминаний iCloud через CalDAV.

    Список («Кубышка — задачи» по умолчанию) должен быть заведён и
    расшарен на команду ЗАРАНЕЕ вручную в приложении Напоминания на
    телефоне владельца Apple ID — публичного API для создания самого
    списка/шаринга не существует, только для записи задач в уже
    существующий список.
    """

    def __init__(self, apple_id: str, app_specific_password: str, list_name: str) -> None:
        self._client = caldav.DAVClient(url=_ICLOUD_CALDAV_URL, username=apple_id, password=app_specific_password)
        self._list_name = list_name
        self._todo_list: caldav.Calendar | None = None

    def _resolve_list(self) -> caldav.Calendar:
        if self._todo_list is not None:
            return self._todo_list
        principal = self._client.principal()
        for calendar in principal.calendars():
            try:
                name = calendar.name
            except Exception:
                continue
            if name == self._list_name:
                self._todo_list = calendar
                return calendar
        raise RemindersListNotFound(
            f"Список напоминаний «{self._list_name}» не найден в iCloud — "
            "проверь, что он создан и название совпадает."
        )

    def add_task(self, title: str, notes: str = "") -> str:
        """Создаёт VTODO-задачу. Возвращает uid созданной записи."""
        todo_list = self._resolve_list()
        uid = str(uuid.uuid4())
        now = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        vtodo = (
            "BEGIN:VCALENDAR\r\n"
            "VERSION:2.0\r\n"
            "PRODID:-//kubyshka-bots//team_bot//RU\r\n"
            "BEGIN:VTODO\r\n"
            f"UID:{uid}\r\n"
            f"DTSTAMP:{now}\r\n"
            f"SUMMARY:{_escape(title)}\r\n"
        )
        if notes:
            vtodo += f"DESCRIPTION:{_escape(notes)}\r\n"
        vtodo += "STATUS:NEEDS-ACTION\r\nEND:VTODO\r\nEND:VCALENDAR\r\n"
        todo_list.add_todo(vtodo)
        return uid

    def list_open_tasks(self) -> list[OpenTask]:
        """Незавершённые задачи списка, отсортированные как в приложении
        (по due/priority — так же, как caldav.Calendar.todos() по умолчанию)."""
        todo_list = self._resolve_list()
        tasks: list[OpenTask] = []
        for todo in todo_list.todos(include_completed=False):
            component = todo.icalendar_component
            title = str(component.get("summary", "(без названия)"))
            uid = str(component.get("uid"))
            tasks.append(OpenTask(uid=uid, title=title))
        return tasks

    def complete_task(self, uid: str) -> str:
        """Отмечает задачу выполненной. Возвращает её название (для ответа
        пользователю). Бросает TaskNotFound, если uid не найден среди
        незавершённых задач списка."""
        todo_list = self._resolve_list()
        for todo in todo_list.todos(include_completed=False):
            component = todo.icalendar_component
            if str(component.get("uid")) == uid:
                title = str(component.get("summary", "(без названия)"))
                todo.complete()
                return title
        raise TaskNotFound(f"Задача с id {uid} не найдена среди незавершённых.")


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")
