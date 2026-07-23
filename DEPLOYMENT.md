# Деплой ботов на сервер Кирилла

До сих пор оба бота (`team_bot`, `channel_bot`) и мини-апп (`webapp`) крутились
только локально на моей машине разработки (`nohup`, временный
`cloudflared`-туннель). Ниже — инструкция для реального постоянного деплоя
на VPS Кирилла по SSH.

## Прежде чем начинать — уточнить у Кирилла

- **Доступ**: IP/хост сервера, логин, пароль (или SSH-ключ). Есть ли `sudo`?
- **ОС**: инструкция ниже — под Ubuntu/Debian (`apt`). Если что-то другое
  (CentOS/Alma) — команды установки пакетов будут отличаться, скажи мне,
  адаптирую.
- **Уже что-то крутится на этом сервере?** В частности — не там ли уже
  задеплоен `Finik-backend` (реальный бэкенд приложения)? Если да — не
  затереть его порты/systemd-юниты/nginx-конфиг.
- **Домен.** Для `webapp` (доска задач, кнопка `/board`) нужен публичный
  HTTPS-адрес — Telegram не откроет `web_app`-кнопку на `http://`/localhost.
  Есть ли у Кирилла домен/поддомен, который можно направить на этот сервер?
  Без домена тоже можно (см. вариант B в шаге 8), но с доменом надёжнее.

## Общая схема

На сервере будет три постоянно работающих процесса + (опционально) прокси:

```
systemd: team_bot.service     → python -m team_bot.main
systemd: channel_bot.service  → python -m channel_bot.main
systemd: kubyshka-webapp.service → uvicorn webapp.server:app
(опционально) Caddy/nginx     → HTTPS-домен → localhost:8080 (webapp)
```

`systemd` вместо голого `nohup`/`screen` — чтобы боты сами поднимались после
перезагрузки/падения сервера, а не требовали ручного перезапуска.

## Шаг 1 — подключение и подготовка сервера

```bash
ssh <юзер>@<ip-сервера>
```

Пароль Кирилла вводится в момент подключения — нигде не сохранять его в
файлах репозитория/чате.

```bash
sudo apt update
sudo apt install -y python3.11 python3.11-venv git
python3.11 --version   # проверить, что реально 3.11+
```

Если в `apt` нет `python3.11` (более старый Debian/Ubuntu) — через
`deadsnakes` PPA:
```bash
sudo add-apt-repository ppa:deadsnakes/ppa -y
sudo apt update && sudo apt install -y python3.11 python3.11-venv
```

## Шаг 2 — перенос кода

Репозиторий `github.com/pokirill/ai_employees` — приватный. Проще всего
сгенерировать SSH-ключ прямо на сервере и добавить его как deploy key в
GitHub (Settings репозитория → Deploy keys), а не тащить свой личный ключ
на чужой сервер:

```bash
ssh-keygen -t ed25519 -C "kubyshka-bots-deploy" -f ~/.ssh/id_ed25519 -N ""
cat ~/.ssh/id_ed25519.pub   # добавить в GitHub → ai_employees → Settings → Deploy keys (read-only достаточно)
```

```bash
mkdir -p ~/apps && cd ~/apps
git clone git@github.com:pokirill/ai_employees.git kubyshka-bots
cd kubyshka-bots
git checkout claude-work-kubyshka-bots-migration   # или main, если ветка уже смержена к тому моменту
```

## Шаг 3 — доки FinAssist/Finik-backend для контекста ассистента

**Важно и легко забыть**: `FINASSIST_DOCS_PATH` — ОБЯЗАТЕЛЬНАЯ переменная
(`shared/config.py`, `_require`) — без неё `team_bot` не запустится вообще,
даже если ассистентом никто не пользуется. На моей машине она указывает на
локальный чекаут FinAssist — на сервере Кирилла такого чекаута нет.

Два варианта:
- **(A, проще)** Скопировать ТОЛЬКО папки `Docs/` (FinAssist) и `docs/`
  (Finik-backend) на сервер — это просто набор `.md`-файлов, полный чекаут
  Xcode-проекта/бэкенда для этого не нужен:
  ```bash
  # с локальной машины (не на сервере!)
  scp -r ~/Documents/FinAssist/Docs <юзер>@<ip-сервера>:~/apps/kubyshka-bots-docs/FinAssist-Docs
  scp -r ~/Desktop/Кубышка/Finik-backend/docs <юзер>@<ip-сервера>:~/apps/kubyshka-bots-docs/Finik-backend-docs
  ```
  Дальше в `.env` на сервере: `FINASSIST_DOCS_PATH=/home/<юзер>/apps/kubyshka-bots-docs/FinAssist-Docs` и
  `FINIK_BACKEND_DOCS_PATH=/home/<юзер>/apps/kubyshka-bots-docs/Finik-backend-docs`.
  Минус — доки не обновляются сами, надо будет периодически перезаливать
  (`rsync -av --delete`, см. ниже).
- **(B)** Полный `git clone` FinAssist/Finik-backend на сервер + периодический
  `git pull` (это уже частично умеет делать сам бот — `sync_docs_repos` в
  `shared/docs_context.py` дёргает `git pull --ff-only` перед каждым
  вопросом ассистенту, если путь — рабочая копия git). Требует deploy key
  и для этих двух репозиториев тоже. Лишний доступ ради данных, которые и
  так не меняются каждый день — для старта рекомендую вариант A.

Плейбук Авито (`AVITO_PLAYBOOK_PATH`) отдельно возить не нужно — он уже
вендорен внутри `ai_employees` (`avito_playbook/docs/`), приедет вместе с
`git clone` на шаге 2.

## Шаг 4 — окружение

```bash
cd ~/apps/kubyshka-bots
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Шаг 5 — .env

**Не присылать токены/ключи в Telegram/чат.** Создать файл прямо на
сервере:

```bash
cp .env.example .env
nano .env   # или vim — заполнить руками
```

Минимум для старта: `OPENAI_API_KEY`, `TEAM_BOT_TOKEN`, `CHANNEL_BOT_TOKEN`,
`CHANNEL_ID`, пути из шага 3, `WEBAPP_URL` (см. шаг 8). Остальное — по
README.md (там расписана каждая переменная).

## Шаг 6 — ОПАСНЫЙ момент: первый запуск `channel_bot`

Если `channel_bot/last_post_state.json` не существует, `channel_bot`
считает, что поста не было ВООБЩЕ никогда, и **опубликует реальный пост в
`@kubyshka_user` сразу при старте**, а не будет ждать расписания. На моей
машине бот ни разу не запускался как реальный процесс именно поэтому — не
хотел рисковать случайной публикацией.

Перед первым запуском на сервере — выбрать одно:
- **(рекомендую для первого раза)** Поставить `CHANNEL_REQUIRE_APPROVAL=1`
  и `CHANNEL_ADMIN_CHAT_ID=<ваш чат>` в `.env` — тогда первый пост уйдёт
  на ревью с кнопками "Опубликовать"/"Пропустить", а не сразу в канал.
  Можно выключить (`=0`) после того, как убедитесь, что всё работает как
  надо.
- Либо создать `channel_bot/last_post_state.json` вручную с недавним
  timestamp — тогда бот подождёт остаток интервала перед первым постом:
  ```bash
  python3 -c "
  import json
  from datetime import datetime, timezone
  json.dump({'last_post_at': datetime.now(timezone.utc).isoformat(), 'last_post_title': ''}, open('channel_bot/last_post_state.json', 'w'))
  "
  ```

## Шаг 7 — systemd, чтобы боты жили постоянно

Три юнита (замени `<юзер>` и путь на реальные):

`/etc/systemd/system/team-bot.service`:
```ini
[Unit]
Description=Kubyshka team_bot
After=network.target

[Service]
Type=simple
User=<юзер>
WorkingDirectory=/home/<юзер>/apps/kubyshka-bots
ExecStart=/home/<юзер>/apps/kubyshka-bots/.venv/bin/python -m team_bot.main
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/channel-bot.service` — то же самое, только
`ExecStart=... -m channel_bot.main` и `Description=Kubyshka channel_bot`.

`/etc/systemd/system/kubyshka-webapp.service`:
```ini
[Unit]
Description=Kubyshka task board webapp
After=network.target

[Service]
Type=simple
User=<юзер>
WorkingDirectory=/home/<юзер>/apps/kubyshka-bots
ExecStart=/home/<юзер>/apps/kubyshka-bots/.venv/bin/python -m uvicorn webapp.server:app --host 127.0.0.1 --port 8080
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`--host 127.0.0.1` (не `0.0.0.0`) — наружу пусть смотрит только
Caddy/nginx с HTTPS (шаг 8), сам uvicorn наружу светить не нужно.

Применить и запустить:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now team-bot channel-bot kubyshka-webapp
sudo systemctl status team-bot channel-bot kubyshka-webapp   # все три active (running)
journalctl -u team-bot -f   # живые логи, Ctrl+C чтобы выйти
```

## Шаг 8 — публичный HTTPS для `/board`

**Вариант A (есть домен) — рекомендую, самый надёжный:**
```bash
sudo apt install -y caddy   # https://caddyserver.com/docs/install
```
`/etc/caddy/Caddyfile`:
```
kubyshka-board.<домен Кирилла> {
    reverse_proxy 127.0.0.1:8080
}
```
Caddy сам получит и продлит Let's Encrypt-сертификат. `WEBAPP_URL` в `.env`
= `https://kubyshka-board.<домен>`.

**Вариант B (домена пока нет) — постоянный cloudflared-туннель** (не
`--url` quick-туннель, как было у меня локально — тот умирает при перезапуске
процесса и ссылка каждый раз новая, для продакшена не годится):
```bash
cloudflared tunnel login
cloudflared tunnel create kubyshka-board
cloudflared tunnel route dns kubyshka-board kubyshka-board.<любой-домен-в-cloudflare-аккаунте>
cloudflared tunnel run kubyshka-board
```
(тоже стоит обернуть в systemd-юнит по тому же шаблону, что выше).

## Шаг 9 — проверка

- `/id` в личке боту → отвечает.
- `/roles` → список из 5 ролей.
- `/ask что такое Кубышка?` → отвечает с контекстом (если пусто — проверить
  `FINASSIST_DOCS_PATH` из шага 3).
- Открыть `/board` в личке → мини-апп открывается, показывает задачи.
- В канале — дождаться (или `/postnow`, если `CHANNEL_REQUIRE_APPROVAL=0`
  и вы уже спокойны за первый пост) реального поста.

## Обновление в будущем

```bash
ssh <юзер>@<ip-сервера>
cd ~/apps/kubyshka-bots
git pull
source .venv/bin/activate
pip install -r requirements.txt   # если requirements.txt менялся
sudo systemctl restart team-bot channel-bot kubyshka-webapp
```

Если доки FinAssist/Finik-backend возились вручную (вариант A шага 3) — их
тоже стоит периодически обновлять тем же `scp -r`/`rsync -av --delete`.
