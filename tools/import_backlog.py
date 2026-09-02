#!/usr/bin/env python3
"""Разбор бэклога: Напоминания и переписка команды → задачи с эпиками.

## Зачем

Задачи команды сейчас лежат в трёх местах: в Напоминаниях, в переписке чата
(«давай сделаем…», «надо не забыть…») и в головах. Пока они там, планировать
нечего: нельзя расставить приоритеты по списку, которого не существует.

Этот скрипт собирает их в одну доску и раскладывает по эпикам.

## Почему по умолчанию ничего не пишет

Запуск без `--apply` только показывает, что БУДЕТ создано. Разбор переписки
неизбежно даёт мусор: реплика «надо бы подумать про Android» — это не задача, а
мысль вслух. Человек должен посмотреть список до того, как он окажется на доске,
иначе доверие к доске закончится на первом же десятке выдуманных задач.

## Как не создать дублей

Сверяем по нормализованному названию: нижний регистр, без знаков препинания, без
слов-паразитов вроде «надо», «нужно», «сделать». Совпало — пропускаем. Это грубо,
но ошибка в сторону пропуска здесь безопаснее: не завести дубль важнее, чем не
потерять формулировку, которую всё равно легко добавить руками.

## Использование

    python3 tools/import_backlog.py --reminders          # посмотреть
    python3 tools/import_backlog.py --reminders --apply  # записать
    python3 tools/import_backlog.py --chat --days 30
    python3 tools/import_backlog.py --reminders --chat --apply
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared import epics  # noqa: E402
from shared.task_store import TaskStore  # noqa: E402

# Слова, которые ничего не добавляют к смыслу задачи и мешают сравнивать
# названия между собой: «надо починить импорт» и «починить импорт» — одно и то же.
_NOISE = (
    "надо", "нужно", "необходимо", "давай", "давайте", "сделать", "сделай",
    "please", "todo", "задача", "срочно", "не забыть", "хочу", "хотелось бы",
    # Частицы отдельно: без них «надо бы починить» и «починить» не совпадали,
    # и дубль всё-таки уезжал на доску.
    " бы ", " же ", " ка ", " вот ", " там ", " ещё ", " еще ",
)

_EXTRACT_PROMPT = (
    "Ниже переписка команды. Выпиши из неё ЗАДАЧИ — то, что кто-то собирался "
    "сделать и что можно взять в работу.\n\n"
    "Строгие правила:\n"
    "- только то, что явно прозвучало как дело; мысли вслух, вопросы и обсуждения — не задачи;\n"
    "- формулируй коротко и по-русски, глаголом: «починить дубли трат», а не «дубли»;\n"
    "- не выдумывай исполнителей и сроки;\n"
    "- если задач нет — верни пустой список.\n\n"
    'Ответ строго JSON: {"tasks": ["...", "..."]}\n\n'
    "Переписка:\n"
)


def normalize(title: str) -> str:
    text = title.lower().replace("ё", "е")
    text = re.sub(r"[^\w\s]", " ", text)
    # Пробелы по краям нужны, чтобы шум вида " бы " ловился и в начале строки.
    text = f" {' '.join(text.split())} "
    for word in _NOISE:
        padded = word if word.startswith(" ") else f" {word} "
        while padded in text:
            text = text.replace(padded, " ")
    return " ".join(text.split())


def load_existing(store: TaskStore) -> set[str]:
    return {normalize(task.title) for task in store.list_tasks(include_done=True)}


def from_reminders(config) -> list[str]:
    """Открытые задачи из общего списка Напоминаний."""
    from shared.icloud_reminders import ICloudReminders

    if not (config.icloud_apple_id and config.icloud_app_password):
        print("Напоминания не настроены: нет ICLOUD_APPLE_ID / ICLOUD_APP_SPECIFIC_PASSWORD")
        return []
    client = ICloudReminders(
        apple_id=config.icloud_apple_id,
        app_specific_password=config.icloud_app_password,
        list_name=config.icloud_reminders_list_name,
    )
    try:
        return [item.title.strip() for item in client.list_open_tasks() if item.title.strip()]
    except Exception as exc:  # noqa: BLE001
        print(f"Не удалось прочитать Напоминания: {exc}")
        return []


def from_chat(path: str, *, days: int, llm) -> list[str]:
    """Задачи из переписки команды за последние `days` дней."""
    from shared.chat_log import format_for_prompt, messages_since

    if not Path(path).exists():
        print(f"Лог переписки не найден: {path}")
        return []
    since = datetime.now(timezone.utc) - timedelta(days=days)
    messages = messages_since(path, since)
    if not messages:
        print("В логе переписки нет сообщений за этот период")
        return []
    if llm is None:
        print("Без ключа модели переписку разобрать нечем (OPENAI_API_KEY)")
        return []

    # Режем на куски: длинная переписка в один запрос не влезет, а обрезать её
    # молча — значит потерять хвост, где обычно и живут свежие договорённости.
    chunks = _chunk(format_for_prompt(messages, max_chars=100_000), size=6000)
    found: list[str] = []
    for index, chunk in enumerate(chunks, 1):
        print(f"   разбираю переписку, часть {index} из {len(chunks)}…")
        try:
            answer = llm.chat(
                [{"role": "user", "content": _EXTRACT_PROMPT + chunk}],
                max_tokens=800,
                temperature=0.0,
            )
            payload = json.loads(_json_slice(answer))
            found.extend(str(item).strip() for item in payload.get("tasks", []) if str(item).strip())
        except Exception as exc:  # noqa: BLE001
            print(f"   часть {index} не разобралась: {exc}")
    return found


def _chunk(text: str, *, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


def _json_slice(answer: str) -> str:
    """Модель иногда оборачивает JSON в пояснения — берём фигурные скобки."""
    start, end = answer.find("{"), answer.rfind("}")
    return answer[start : end + 1] if start >= 0 and end > start else "{}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Собрать бэклог из Напоминаний и переписки")
    parser.add_argument("--reminders", action="store_true", help="взять открытые Напоминания")
    parser.add_argument("--chat", action="store_true", help="разобрать переписку команды")
    parser.add_argument("--days", type=int, default=30, help="за сколько дней смотреть переписку")
    parser.add_argument("--apply", action="store_true", help="записать на доску (без этого — только показать)")
    args = parser.parse_args()

    if not (args.reminders or args.chat):
        parser.error("укажи хотя бы один источник: --reminders и/или --chat")

    from shared.config import LLMConfig, TaskBoardConfig, TeamBotConfig
    from shared.llm_client import LLMClient

    config = TeamBotConfig()
    board_config = TaskBoardConfig()
    store = TaskStore(board_config.db_path)
    llm_config = LLMConfig()
    llm = LLMClient(llm_config, model_override=config.model_override) if llm_config.api_key else None

    candidates: list[tuple[str, str]] = []
    if args.reminders:
        for title in from_reminders(config):
            candidates.append((title, "reminders"))
    if args.chat:
        from shared.chat_log import DEFAULT_LOG_PATH

        chat_path = DEFAULT_LOG_PATH
        for title in from_chat(chat_path, days=args.days, llm=llm):
            candidates.append((title, "chat"))

    existing = load_existing(store)
    fresh: list[tuple[str, str, str]] = []
    skipped = 0
    seen: set[str] = set()

    for title, source in candidates:
        key = normalize(title)
        if not key or key in existing or key in seen:
            skipped += 1
            continue
        seen.add(key)
        fresh.append((title, source, epics.classify(title, llm=llm)))

    print()
    print(f"Найдено: {len(candidates)}, из них новых: {len(fresh)}, пропущено как дубли: {skipped}")
    print()

    by_epic: dict[str, list[tuple[str, str]]] = {}
    for title, source, epic in fresh:
        by_epic.setdefault(epic, []).append((title, source))

    for code, items in sorted(by_epic.items(), key=lambda item: -len(item[1])):
        print(f"{epics.label(code)} — {len(items)}")
        for title, source in items:
            print(f"    • {title}   [{source}]")
        print()

    if not args.apply:
        print("Это предпросмотр. Чтобы записать на доску, добавь --apply")
        return 0

    for title, source, epic in fresh:
        store.add_task(title, created_by="импорт", epic=epic, origin=source)
    print(f"Записано на доску: {len(fresh)} задач.")
    print("Дальше: /backlog в боте — расставить приоритеты, /startsprint — открыть спринт.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
