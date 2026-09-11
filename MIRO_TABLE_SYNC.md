# Team Helper ↔ Miro Table

## Что является источником правды

`kubyshka_tasks.db` остаётся ядром системы. Telegram/mini-app, Miro Table и
Reminders — интерфейсы к тем же задачам. Исключение только первое подключение
уже существующей Miro-таблицы: её видимые поля импортируются один раз, потому
что команда меняла Miro в период, когда старый Card/Frame sync эту таблицу не
видел.

После привязки действует правило:

- изменили только Miro → изменение приходит в SQLite;
- изменили только Team Helper → изменение уходит в Miro;
- изменили обе стороны до следующего sync → SQLite/Team Helper выигрывает,
  бот сообщает о конфликте;
- удаление строки в Miro не удаляет задачу: строка создаётся заново.

Синхронизация запускается существующим циклом Team Helper (раз в 5 минут) и
вручную через `/syncnow`.

## Текущая таблица

```env
MIRO_TABLE_URL=https://miro.com/app/board/uXjVHr7ManQ=/?moveToWidget=3458764682502106281
MIRO_TABLE_SPRINT_STATUS=Спринт 1
```

Поддерживаемые колонки: `Title`, `Description`, `Priority`, `Estimate`,
`Assignee`, `Tags`, `Status`.

Статусы:

- `Бэклог` → задача вне спринта;
- `Спринт 1` (настраивается `MIRO_TABLE_SPRINT_STATUS`) → в текущем спринте,
  но работа ещё не начата;
- `В работе` → работа начата;
- `На проверке` → `testing`;
- `Готово` → `done`.

`Assignee` и `В работе` независимы. Поэтому задача может быть назначена Саше и
при этом оставаться в колонке спринта до фактического `/claim` или перемещения
в `В работе`.

## Связь строк и задач

Miro `rowId` хранится в `task_sync` с target=`miro_table` и дублируется в
`tasks.miro_item_id` для совместимости.

- существующая строка `#157 · ...` связывается именно с task id 157;
- новая строка без номера создаёт новую задачу в SQLite, после чего Miro Title
  канонизируется в `#<id> · <title>`;
- локальная задача без строки создаётся в Miro и затем связывается по `#id`.

Tags не затираются при обычном Team Helper → Miro update: в Miro возможны
несколько тегов, а SQLite пока хранит один `epic`. При создании строки epic
пишется как tag; при чтении Miro первый известный tag обновляет epic.

## OAuth Miro MCP

Для Table CRUD используется официальный remote MCP endpoint.

```env
MIRO_MCP_SERVER_URL=https://mcp.miro.com/
MIRO_MCP_AUTH_FILE=.miro_mcp_auth.json
MIRO_MCP_REDIRECT_URI=http://127.0.0.1:8765/callback
```

Используйте отдельного Miro-пользователя `Team Helper` с Editor-доступом к
доске. Не авторизуйте сервер под личным Miro-пользователем: Miro MCP connection
team-specific, и повторная MCP-авторизация того же пользователя может выбить
его подключение из другого AI-клиента.

Одноразовая настройка:

```bash
pip install -r requirements.txt
python tools/miro_mcp_auth.py
```

Скрипт откроет/покажет OAuth URL. Выберите Miro team с доской Кубышки, после
redirect вставьте полный callback URL обратно в терминал. Получившийся
`.miro_mcp_auth.json` содержит refresh token и client registration; хранить как
секрет, не коммитить.

Затем на сервере Team Helper:

1. положить OAuth-файл и выставить `MIRO_MCP_AUTH_FILE`;
2. выставить `MIRO_TABLE_URL` и остальные env выше;
3. обновить зависимости и код;
4. рестартовать Team Helper;
5. выполнить `/syncnow`.

Если OAuth истёк/отозван, `/syncnow` вернёт явную ошибку и предложит снова
запустить `tools/miro_mcp_auth.py`.

## Acceptance / DoD

1. Создать в Miro строку `Тест Miro → бот` без `#ID`.
2. `/syncnow` → строка получает `#ID`, задача видна в Team Helper.
3. Поменять Priority/Description/Assignee в Miro → `/syncnow` → поля совпали в
   Team Helper.
4. Переместить в `На проверке` → `/syncnow` → task status=`testing`.
5. Создать `/task Тест бот → Miro` → `/syncnow` → появилась строка Miro.
6. `/claim <id>` → `/syncnow` → `Assignee` заполнен и Status=`В работе`.
7. `/done <id>` → `/syncnow` → Status=`Готово`.
8. Удалить тестовую строку Miro → `/syncnow` → SQLite-задача не исчезла,
   строка восстановлена.
9. Повторный `/syncnow` без новых правок даёт 0 изменений и не создаёт дублей.
