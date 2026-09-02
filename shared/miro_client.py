"""Канбан-доска в Miro (TASK-SYS-1).

## Что Miro в этой системе

Витрина, а не хранилище. На доске лежат карточки задач, разложенные по четырём
колонкам-фреймам, и человек двигает бумажку мышкой — это единственное, ради
чего Miro здесь нужен. Всё остальное (исполнитель, комментарии, оценки, эпик,
история) живёт в SQLite, потому что в карточку Miro это не помещается и, что
важнее, не переживёт случайное удаление карточки.

## Колонки — фреймы, а не координаты

Фрейм в Miro — контейнер: карточка, брошенная внутрь, становится его ребёнком,
и это видно в API как `parent.id`. Если бы колонки были просто нарисованными
прямоугольниками, определять «в какой колонке карточка» пришлось бы по
координатам — и любое случайное перетаскивание доски ломало бы синхронизацию.

## Про токен

Нужен токен приложения Miro с правами `boards:read` и `boards:write`. Пока его
нет, класс не создаётся: `enabled` возвращает False, и вся синхронизация с Miro
просто выключена. Это не ошибка и не деградация — доска в боте и Напоминания
работают сами по себе.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://api.miro.com/v2"
_TIMEOUT = 20.0

# Колонки доски. Порядок важен: в этом же порядке они рисуются слева направо.
# Ключ — статус задачи в нашей базе, значение — подпись фрейма.
COLUMNS: tuple[tuple[str, str], ...] = (
    ("open", "Бэклог"),
    ("claimed", "В работе"),
    ("testing", "Тестируется"),
    ("done", "Готово"),
)

COLUMN_TITLES = {status: title for status, title in COLUMNS}
STATUS_BY_TITLE = {title: status for status, title in COLUMNS}

# Геометрия. Числа подобраны так, чтобы четыре колонки помещались на экран
# без прокрутки и между ними оставался зазор для перетаскивания.
_FRAME_WIDTH = 420
_FRAME_HEIGHT = 1400
_FRAME_GAP = 60
_CARD_WIDTH = 320


@dataclass(frozen=True)
class MiroCard:
    item_id: str
    title: str
    description: str
    frame_id: str | None
    status: str | None


class MiroError(RuntimeError):
    pass


class MiroBoard:
    """Доска спринта в Miro.

    Одна доска на спринт: смешивать спринты на одном полотне — значит через
    месяц не найти на нём ничего. Идентификатор доски хранится в спринте,
    а не в настройках.
    """

    def __init__(self, token: str, board_id: str) -> None:
        self._token = token.strip()
        self._board_id = board_id.strip()

    @property
    def enabled(self) -> bool:
        return bool(self._token and self._board_id)

    # ------------------------------------------------------------------
    # Структура доски
    # ------------------------------------------------------------------

    def ensure_columns(self) -> dict[str, str]:
        """Создаёт недостающие колонки и возвращает {статус: id фрейма}.

        Идемпотентно: колонку узнаём по подписи. Если человек переименовал
        фрейм, мы создадим новый рядом — и это правильнее, чем угадывать:
        переименованный фрейм мог стать чем-то другим по смыслу.
        """
        existing = {}
        for frame in self._get_items(item_type="frame"):
            title = (frame.get("data") or {}).get("title", "")
            if title in STATUS_BY_TITLE:
                existing[STATUS_BY_TITLE[title]] = frame["id"]

        for index, (status, title) in enumerate(COLUMNS):
            if status in existing:
                continue
            x = index * (_FRAME_WIDTH + _FRAME_GAP)
            created = self._post(
                f"/boards/{self._board_id}/frames",
                {
                    "data": {"title": title, "format": "custom", "type": "freeform"},
                    "position": {"x": x, "y": 0},
                    "geometry": {"width": _FRAME_WIDTH, "height": _FRAME_HEIGHT},
                },
            )
            existing[status] = created["id"]
            logger.info("miro: создана колонка %s", title)
        return existing

    # ------------------------------------------------------------------
    # Карточки
    # ------------------------------------------------------------------

    def list_cards(self) -> list[MiroCard]:
        frames = {}
        for frame in self._get_items(item_type="frame"):
            title = (frame.get("data") or {}).get("title", "")
            frames[frame["id"]] = STATUS_BY_TITLE.get(title)

        cards: list[MiroCard] = []
        for item in self._get_items(item_type="card"):
            data = item.get("data") or {}
            parent_id = (item.get("parent") or {}).get("id")
            cards.append(
                MiroCard(
                    item_id=item["id"],
                    title=data.get("title", "").strip(),
                    description=data.get("description", "") or "",
                    frame_id=parent_id,
                    status=frames.get(parent_id),
                )
            )
        return cards

    def create_card(self, *, title: str, description: str, frame_id: str, index: int) -> str:
        payload = {
            "data": {"title": title[:400], "description": description[:6000]},
            "position": {"x": 0, "y": 0},
            "geometry": {"width": _CARD_WIDTH},
            "parent": {"id": frame_id},
        }
        created = self._post(f"/boards/{self._board_id}/cards", payload)
        return created["id"]

    def update_card(self, item_id: str, *, title: str, description: str, frame_id: str) -> None:
        self._patch(
            f"/boards/{self._board_id}/cards/{item_id}",
            {
                "data": {"title": title[:400], "description": description[:6000]},
                "parent": {"id": frame_id},
            },
        )

    def delete_card(self, item_id: str) -> None:
        self._delete(f"/boards/{self._board_id}/cards/{item_id}")

    # ------------------------------------------------------------------
    # Транспорт
    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}

    def _get_items(self, *, item_type: str) -> list[dict]:
        """Все элементы одного типа. Miro отдаёт страницами — идём по курсору
        до конца: на доске спринта карточек больше пятидесяти бывает регулярно,
        и молча потерять хвост означает создать их заново дублями."""
        items: list[dict] = []
        cursor = None
        while True:
            params = {"type": item_type, "limit": 50}
            if cursor:
                params["cursor"] = cursor
            payload = self._get(f"/boards/{self._board_id}/items", params)
            items.extend(payload.get("data") or [])
            cursor = payload.get("cursor")
            if not cursor:
                return items

    def _get(self, path: str, params: dict | None = None) -> dict:
        return self._request("GET", path, params=params)

    def _post(self, path: str, body: dict) -> dict:
        return self._request("POST", path, json=body)

    def _patch(self, path: str, body: dict) -> dict:
        return self._request("PATCH", path, json=body)

    def _delete(self, path: str) -> dict:
        return self._request("DELETE", path)

    def _request(self, method: str, path: str, **kwargs) -> dict:
        if not self.enabled:
            raise MiroError("Miro не настроен: нет токена или id доски")
        try:
            response = httpx.request(
                method, f"{API_BASE}{path}", headers=self._headers(), timeout=_TIMEOUT, **kwargs
            )
        except Exception as exc:  # noqa: BLE001 - сеть
            raise MiroError(f"Miro недоступен: {exc}") from exc

        if response.status_code == 401:
            raise MiroError("Miro отклонил токен — проверь права boards:read и boards:write")
        if response.status_code == 404:
            raise MiroError("Доска Miro не найдена — проверь id доски")
        if response.status_code == 429:
            # Лимит запросов. Отдельно, потому что это не поломка: следующий
            # проход синхронизации доделает то, что не успели сейчас.
            raise MiroError("Miro просит подождать: слишком много запросов")
        if response.status_code >= 400:
            raise MiroError(f"Miro ответил {response.status_code}: {response.text[:200]}")
        if not response.content:
            return {}
        return response.json()
