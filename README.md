
---

# Timeline Notifier

Telegram-бот, который следит за дедлайнами работ в **Timeline УрГПУ** сразу за многими студентами.
Студент один раз пишет боту в личку свой логин/пароль ЭИОС — дальше бот сам логинится на
`timeline.rsvpu.ru` и присылает **ему** напоминания: новые работы, смену статуса, «осталось
48/24/12 часов», просрочку. Веб-морда — **админ-панель**, студенты ей не пользуются.

Никакой общей базы студентов: у каждого пользователя бота своя сессия в Timeline, свой набор
работ (`user_work_item.user_id`) и свои уведомления только в его чат.

> **Токен бота — это доступ к боту целиком.** Он живёт только в БД (`setting.telegram_bot_token`)
> и задаётся из админки; в репозиторий, в `.env`, в чаты и скриншоты он не попадает (`.env` и
> `instance/` — в `.gitignore`). Логины и пароли студентов в репозитории не хранятся вообще:
> их бот собирает в личке и держит в БД зашифрованными. Если токен всё же утёк — @BotFather →
> `/mybots` → **Revoke current token**, затем вписать новый в админке.

---

## Содержание

1. [Как это работает](#1-как-это-работает)
2. [Структура проекта](#2-структура-проекта)
3. [Передача проекта на сервер](#передача-проекта-на-сервер)
4. [Установка на сервер (Ubuntu, пошагово)](#3-установка-на-сервер-ubuntu-пошагово)
5. [Docker (вариант, который просил Андрей)](#docker-вариант-который-просил-андрей)
6. [Telegram из РФ: прокси](#4-telegram-из-рф-прокси)
7. [Справочник `.env`](#5-справочник-env)
8. [Как пользоваться](#6-как-пользоваться)
9. [База данных и бэкапы](#7-база-данных-и-бэкапы)
10. [Диагностика: «не работает»](#8-диагностика-не-работает)
11. [Как дорабатывать код](#9-как-дорабатывать-код)
12. [Ограничения и куда развивать](#10-ограничения-и-куда-развивать)

---

## 1. Как это работает

```
                        ┌──────────────────── один процесс (run.py) ────────────────────┐
  студент в TG ────────▶│ app/bot/telegram_bot.py — long-polling в отдельном потоке      │
  /start → логин → пароль│   проверяет пару через app/parser/auth.py                    │
                        │   создаёт bot_user (chat_id, login, password_enc)             │
                        └──────────────────────┬───────────────────────────────────────┘
                                               │ каждые SCHEDULER_TICK сек
                        ┌──────────────────────▼───────────────────────────────────────┐
                        │ app/scheduler/jobs.py                                         │
                        │   check_all_users(): кому пора (свой check_interval)?         │
                        │   → ThreadPoolExecutor(CHECK_WORKERS) → check_user(user)      │
                        │       auth.login() → scraper.fetch_works()                    │
                        │       diff с user_work_item → notify_* в чат ЭТОГО юзера      │
                        └──────────────────────┬───────────────────────────────────────┘
                                               │
  админ в браузере ────▶│ app/routes/web.py — Flask + Basic Auth: юзеры, кнопки, настройки
                        └──────────────────────────────────────────────────────────────┘
```

Что происходит по шагам:

1. Студент жмёт `/start`. Бот спрашивает логин, потом пароль (сообщение с паролем **удаляется**
   из чата через `deleteMessage`).
2. Бот проверяет пару реальным входом в ЭИОС. Успех → `bot_user.is_registered = True`,
   пароль шифруется (`app/security.py`, Fernet) и кладётся в `password_enc`.
3. Планировщик раз в `SCHEDULER_TICK` берёт всех активных зарегистрированных, у кого
   `now - last_check_at >= check_interval`, и проверяет их параллельно.
4. `check_user()`: логин → список работ из `SendGrid` → сравнение с тем, что уже в
   `user_work_item` → уведомления:
   * новая работа (кроме уже отправленных/зачтённых);
   * смена статуса;
   * 48 / 24 / 12 часов до дедлайна (по одному разу, флаги `reminder_*_sent`);
   * просрочка (`overdue_sent`).
5. Работа, пропавшая из ведомости, удаляется из БД; дата дедлайна сбрасывает флаги напоминаний.
6. Если вход перестал работать (сменил пароль) — пользователю один раз уходит «смени логин/пароль»
   и пишется `last_error`; в админке это видно.

---

## 2. Структура проекта

```
timeline_notifier/
├── run.py                      # точка входа: create_app + планировщик + бот + веб-сервер
├── start.bat                   # запуск на Windows (локальная отладка)
├── Dockerfile                  # образ: python 3.12 + зависимости + процесс не от root
├── docker-compose.yml          # запуск контейнера: том ./instance, порт 5000, restart
├── .dockerignore               # что не кладём в образ (.env, instance/, venv/)
├── requirements.txt            # зависимости
├── .env.example                # шаблон конфига → копируется в .env
├── app/
│   ├── __init__.py             # create_app(): конфиг, БД, регистрация роутов
│   ├── config.py               # ВСЕ настройки из окружения (.env)
│   ├── models.py               # таблицы: Setting, User (bot_user), WorkItem (user_work_item)
│   ├── security.py             # шифрование/расшифровка паролей ЭИОС
│   ├── bot/
│   │   ├── telegram_bot.py     # Telegram Bot API, polling, меню, онбординг, тексты уведомлений
│   │   └── jokes.py            # все шутки бота по разделам (правь тут)
│   ├── parser/
│   │   ├── auth.py             # вход в ЭИОС (OAuth-цепочка, форма, cookie-сессия)
│   │   └── scraper.py          # список работ из SendGrid + разбор полей
│   ├── scheduler/
│   │   └── jobs.py             # кто когда проверяется, diff, напоминания 48/24/12, просрочка
│   ├── routes/
│   │   └── web.py              # админ-панель (Blueprint web_bp)
│   ├── templates/              # base.html, index.html (юзеры), user.html (карточка)
│   └── static/style.css
├── instance/                   # рантайм-данные (в git не попадают)
│   ├── timeline.db             # SQLite, создаётся автоматически
│   └── debug/                  # дампы страниц/JSON, если TIMELINE_DEBUG_DUMP=true
├── deploy/
│   ├── timeline-notifier.service   # systemd-юнит приложения
│   ├── tg-ws-proxy.service         # systemd-юнит SOCKS5-прокси для Telegram
│   └── nginx-example.conf          # опционально: HTTPS + домен
├── docs/
│   ├── parser-notes.md             # как устроен парсер Timeline и как его чинить
│   └── sample_sendgrid_response.json  # образец реального ответа SendGrid
├── tests/
│   └── smoke_test.py           # прогон всей логики на временной БД, без сети
└── tools/
    ├── pack.ps1                # чистый архив проекта для передачи (без .env/venv/instance)
    └── healthcheck.py          # проверка живости для docker HEALTHCHECK
```

### Куда лезть за чем

| Что нужно сделать | Файл |
|---|---|
| Поменять текст уведомления, кнопки меню, команды | `app/bot/telegram_bot.py` |
| Поменять шутки/подписи-подъёбки | `app/bot/jokes.py` |
| Поменять пороги 48/24/12 ч, порядок проверки, интервалы | `app/scheduler/jobs.py` |
| Починить вход в ЭИОС / разбор списка работ | `app/parser/auth.py`, `app/parser/scraper.py` → [docs/parser-notes.md](docs/parser-notes.md) |
| Добавить поле в БД, поменять схему | `app/models.py` (и пересоздать `instance/timeline.db`) |
| Добавить настройку из окружения | `app/config.py` + `.env.example` |
| Поменять админку | `app/routes/web.py` + `app/templates/*` |
| Понять, почему упало | логи: `journalctl -u timeline-notifier -f` |

---

## Передача проекта на сервер

Передавать нужно **только код**. Что бы ты ни выбрал, в посылку не должны попасть:
`.env` (там `SECRET_KEY`/`CRED_KEY`), `instance/` (база с паролями студентов и debug-дампы),
`venv/` (он под Windows и на Linux бесполезен), `__pycache__/`.

**Вариант 1 (рекомендую): git.** Приватный репозиторий, на сервере `git clone`, обновления —
`git pull` + `systemctl restart timeline-notifier`. `.gitignore` уже настроен, секреты и база
не уедут. Если репозитория ещё нет:

```bash
cd timeline_notifier
git init && git add . && git commit -m "Timeline Notifier"
git remote add origin git@github.com:you/timeline-notifier.git
git push -u origin HEAD
```

**Вариант 2: архив.** Собрать чистыми (без `.env`, `venv`, `instance`) одной командой:

```powershell
# Windows: создаст ..\timeline_notifier.zip (31 файл, ~67 КБ)
powershell -ExecutionPolicy Bypass -File tools\pack.ps1
```

```bash
# Linux/macOS: то же самое через tar
tar --exclude=venv --exclude=instance --exclude=.env --exclude=__pycache__ \
    --exclude=.git -czf ../timeline-notifier.tar.gz -C .. timeline_notifier
```

Скрипт `tools/pack.ps1` сам проверяет, что в архив не попали `.env`, `instance/` и `venv/`,
и пишет об этом в конце. Полученный архив кидай Андрею любым способом (файл, облако, scp).

**Вариант 3: scp напрямую**, если сервер уже есть:

```bash
# файлы — без мусора; .env Андрей создаёт сам на сервере
rsync -av --exclude venv --exclude instance --exclude .env --exclude __pycache__ \
      timeline_notifier/ andrey@server:/opt/timeline_notifier/
```

### Что сказать Андрею отдельно (не в архиве!)

* **Токен бота** — не в архиве, не в переписке, не в README. Он вводится на сервере в админке
  (`http://<сервер>:5000/` → «Telegram Bot Token» → «Сохранить»), либо передаётся лично/через
  самоуничтожающееся сообщение. Он же потом может отозвать токен у @BotFather.
* **Логины/пароли студентов** ему знать не нужно вообще: их бот собирает сам в личке и хранит
  зашифрованными. Если он получил базу — скажи, что `CRED_KEY` от неё он тоже не получит.
* **Свои `SECRET_KEY` и `CRED_KEY`** Андрей генерирует на сервере сам (команда в 3.4).
  Не пересылай ему свои — иначе один и тот же ключ шифрования гуляет по чатам.

### Мини-чеклист для Андрея

1. Прочитать «Сначала грабли» ниже (пункт 3.0) — там про 409, пароль админки и прокси.
2. **Если он хочет Docker** (а он хочет): `cp .env.example .env` → вписать ключи и
   `ADMIN_PASSWORD` → `sudo chown -R 10001:10001 instance` → `docker compose up -d --build`.
   Подробности и грабли — в разделе [Docker](#docker-вариант-который-просил-андрей).
3. **Если нативно:** `venv` + `pip install -r requirements.txt`, `.env`, `python run.py`.
4. В любом варианте: открыть админку, вставить токен, написать боту `/start` самому себе,
   убедиться, что работы подтянулись, и только потом ставить на автозапуск (systemd/restart).

---

## 3. Установка на сервер (Ubuntu, пошагово)

Проверялось на Ubuntu 22.04/24.04, Python 3.10+ (нужен из-за синтаксиса `X | None`).
Дальше все команды — от пользователя с `sudo`.

> ### ⚠️ 3.0. Сначала грабли (прочитай до копирования файлов)
>
> 1. **Погаси локальный инстанс бота.** Если где-то (на твоём ноуте, в старой папке) уже
>    запущен `run.py` с тем же токеном — Telegram будет отдавать апдейты только одному из двух,
>    и на сервере бот не увидит ни одной команды: в логе будет
>    `409 Conflict: terminated by other getUpdates`. Останови лишний процесс или возьми
>    отдельный токен на время отладки. Симптом «уведомления приходят, а команды не работают» —
>    это ровно он.
> 2. **Не копируй `.env`, `venv/`, `instance/`.** Локальный `.env` — с твоими ключами и, скорее
>    всего, с **пустым `ADMIN_PASSWORD`** (админка без пароля!). На сервере: свежие ключи,
>    обязательный пароль на админку, пустая база. Подробности — в разделе «Передача проекта».
> 3. **Проверь Telegram именно с сервера:** `curl -s "https://api.telegram.org/bot<TOKEN>/getMe"`.
>    Если ответа нет — сначала прокси/мирор ([раздел 4](#4-telegram-из-рф-прокси)), потом всё
>    остальное. В логе это выглядит как `Не удалось получить getMe`.
> 4. **Часовой пояс, Python и один процесс.** `timedatectl set-timezone Europe/Moscow` (иначе
>    напоминания съедут), Python ≥ 3.10, и **никаких** `gunicorn -w 4` — каждый воркер поднимет
>    свой long-polling и планировщик.
> 5. **Нагрузка.** Проверено живьём на одном студенте. Сайт УрГПУ уже рвал соединения
>    (`ConnectionResetError 10054`) — если студентов десятки, поставь `CHECK_WORKERS=2`,
>    `DEFAULT_CHECK_INTERVAL=600`, `LOGIN_ATTEMPTS=4`. Бот теперь не пугает людей ложным
>    «смени пароль», но лезть в сайт чаще, чем он выдерживает, всё равно не надо.
> 6. **Чего в проекте нет:** миграций БД (схема меняется пересозданием `instance/timeline.db`),
>    нескольких процессов, HTTPS из коробки (nginx — в `deploy/`). И он **не проверялся на
>    Linux**: конфиги в `deploy/` написаны, но запускались только на Windows. Если что-то
>    не заведётся — это скорее systemd/прокси/права, чем код.
> 7. **Поднимаешь в докере?** Там свои две грабли: права на `./instance` (нужен
>    `chown 10001:10001`) и «один токен = один контейнер». См. раздел
>    [Docker](#docker-вариант-который-просил-андрей).


### 3.1. Пакеты и часовой пояс

```bash
sudo apt update && sudo apt install -y python3-venv python3-pip git tzdata
sudo timedatectl set-timezone Europe/Moscow     # важно: дедлайны в локальном времени
timedatectl                                      # Time zone: Europe/Moscow (MSK, +0300)
```

### 3.2. Пользователь и код

```bash
sudo useradd -r -m -d /opt/timeline_notifier -s /usr/sbin/nologin timeline
sudo -u timeline git clone <URL-репозитория> /opt/timeline_notifier   # или scp -r
cd /opt/timeline_notifier
```

### 3.3. Виртуальное окружение и зависимости

```bash
sudo -u timeline python3 -m venv venv
sudo -u timeline venv/bin/pip install --upgrade pip
sudo -u timeline venv/bin/pip install -r requirements.txt
```

### 3.4. Конфиг `.env`

```bash
sudo -u timeline cp .env.example .env
# сгенерировать два ключа
sudo -u timeline python3 -c "import secrets; print('SECRET_KEY='+secrets.token_hex(32)); print('CRED_KEY='+secrets.token_hex(32))"
sudo -u timeline nano .env
sudo chmod 600 .env          # там пароли/ключи — доступ только владельцу
```

Минимум, что надо вписать:

```env
SECRET_KEY=<сгенерированный>
CRED_KEY=<сгенерированный>       # НЕ менять после запуска!
ADMIN_USER=admin
ADMIN_PASSWORD=<пароль на админку>
ADMIN_CHAT_ID=<твой telegram id> # узнать у @userinfobot
```

> `CRED_KEY` — ключ шифрования паролей студентов. Если его потерять/сменить, все сохранённые
> пароли станут нечитаемыми, и людям придётся заново прислать логин/пароль.

### 3.5. Первый запуск и токен бота

```bash
sudo -u timeline venv/bin/python run.py
# в логе: >>> Админка: http://0.0.0.0:5000/
```

1. Открыть `http://<IP-сервера>:5000/` (логин/пароль из `ADMIN_USER`/`ADMIN_PASSWORD`).
2. Вписать **Telegram Bot Token** от [@BotFather](https://t.me/BotFather), оставить галку
   «Перезапустить бота с новым токеном», сохранить.
3. В логе должно появиться `Бот: @твой_бот (Имя)`. Если там `Не удалось получить getMe` —
   читай [раздел про прокси](#4-telegram-из-рф-прокси).
4. Написать боту `/start` — должен ответить и попросить логин.
5. `Ctrl+C`.

### 3.6. systemd (автозапуск)

```bash
sudo cp deploy/timeline-notifier.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now timeline-notifier
systemctl status timeline-notifier
journalctl -u timeline-notifier -f          # смотреть логи в реальном времени
```

Юнит запускает `/opt/timeline_notifier/venv/bin/python run.py` под пользователем `timeline`.
Если ставил в другой каталог — поправь пути в `deploy/timeline-notifier.service`.

### 3.7. Nginx + HTTPS (опционально)

Ставить не обязательно: порт 5000 уже закрыт Basic Auth. Но если нужен домен и HTTPS:

```bash
sudo apt install -y nginx
sudo cp deploy/nginx-example.conf /etc/nginx/sites-available/timeline-notifier
sudo nano /etc/nginx/sites-available/timeline-notifier     # server_name
sudo ln -s /etc/nginx/sites-available/timeline-notifier /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d timeline.example.com
sudo ufw allow 80,443/tcp
```

Тогда в `.env` поставь `WEB_HOST=127.0.0.1` (чтобы порт 5000 не торчал наружу) и
`sudo systemctl restart timeline-notifier`.

### 3.8. Чек-лист «всё работает»

- [ ] `systemctl status timeline-notifier` → `active (running)`
- [ ] в логе есть `Бот: @username` и `Планировщик запущен: тик 60 сек`
- [ ] бот отвечает на `/start`, принимает логин/пароль, пишет «Данные приняты»
- [ ] в админке появился пользователь, в его карточке видны работы
- [ ] `journalctl -u timeline-notifier -f` показывает `Проверка chat_id=...: работ N`
- [ ] тестовое уведомление: в админке в карточке пользователя кнопка «Тест Telegram»

### 3.9. Обновление версии

```bash
cd /opt/timeline_notifier
sudo cp instance/timeline.db ~/timeline-backup-$(date +%F).db   # бэкап!
sudo -u timeline git pull
sudo -u timeline venv/bin/pip install -r requirements.txt
sudo systemctl restart timeline-notifier
```

Если менялась схема БД (`app/models.py`), читай [раздел 7](#7-база-данных-и-бэкапы).

---

## Docker (вариант, который просил Андрей)

Один контейнер = бот + планировщик + админка. Python и зависимости внутри образа, так что на
сервере ничего, кроме Docker, ставить не нужно. Схема та же, что и при нативной установке:
одна строка процесса, база на томе, тот же `.env`.

### Быстрый старт

```bash
git clone <репозиторий> /opt/timeline_notifier && cd /opt/timeline_notifier
cp .env.example .env
nano .env                       # SECRET_KEY, CRED_KEY, ADMIN_USER/ADMIN_PASSWORD, ADMIN_CHAT_ID

id -u                           # если не 1000 — впиши это число в .env как APP_UID (и id -g как APP_GID)
mkdir -p instance

docker compose up -d --build
docker compose logs -f                  # ждём «Планировщик запущен» и «Бот: @...»
```

Дальше открыть `http://<сервер>:5000/` → вставить **Telegram Bot Token** → «Сохранить»
(галка «Перезапустить бота»). Потом написать боту `/start` самому себе и убедиться, что работы
подтянулись.

### Что где живёт

| Что | Где | Зачем |
|---|---|---|
| Образ | `timeline-notifier:latest` | код, Python 3.12, зависимости; внутри процесс не от root (uid 10001) |
| Настройки | `.env` (в образ не попадает) | `SECRET_KEY`, `CRED_KEY`, пароль админки, прокси, интервалы |
| База, логи, дампы | папка `./instance` на хосте | переживают пересборку образа; бэкап = копирование папки |
| Порт | `5000:5000` в `docker-compose.yml` | админка; за nginx поставь `127.0.0.1:5000:5000` |
| Healthcheck | `tools/healthcheck.py` | `docker compose ps` показывает `healthy` (200 или 401 = живой) |

### Команды на каждый день

```bash
docker compose ps                      # статус и healthy
docker compose logs -f bot             # логи: getMe, проверки, ошибки
docker compose restart bot             # перезапуск
docker compose down                    # остановить (база в ./instance остаётся)
docker compose up -d --build           # обновление после git pull
docker compose exec bot date           # проверить время/TZ внутри контейнера
```

### Обновление версии

```bash
cd /opt/timeline_notifier
cp instance/timeline.db ~/timeline-$(date +%F).db     # бэкап на всякий
git pull
docker compose up -d --build
```

### Бэкап и восстановление базы

```bash
docker compose stop bot
cp instance/timeline.db ~/timeline-backup.db          # восстановление — обратная копия
docker compose start bot
```

Никакого `sudo`: контейнер пишет в `./instance` от твоего пользователя.

### Грабли именно в докере

1. **Права на `./instance`.** Контейнер пишет в эту папку от uid/gid, заданного в
   `docker-compose.yml` (`APP_UID`/`APP_GID`, по умолчанию `1000:1000` — типичный первый
   пользователь Ubuntu). Если твой uid другой (`id -u`), контейнер не сможет создать базу и
   приложение упадёт на старте с понятным текстом — впиши свои числа в `.env`:
   ```env
   APP_UID=1001
   APP_GID=1001
   ```
   Тогда никаких `chown` и `sudo` не нужно: база принадлежит тебе, бэкап — обычное копирование.
   Альтернатива (если хочется гонять контейнер от встроенного пользователя 10001): убрать
   `user:` из compose и сделать `sudo chown -R 10001:10001 instance`.
2. **Один инстанс на один токен.** Нельзя одновременно держать контейнер и локальный
   `python run.py` с тем же токеном: Telegram отдаёт апдейты только одному, во втором будет
   `409 Conflict` и бот не увидит ни одной команды. Это же правило про `docker compose up
   --scale bot=2` — планировщик и поллинг удвоятся, уведомления пойдут дублями.
3. **Прокси для Telegram.** Если `api.telegram.org` с сервера недоступен (РФ), подними
   tg-ws-proxy на **хосте** (systemd-юнит в `deploy/`), в `.env` добавь
   `TELEGRAM_PROXY=socks5h://host.docker.internal:1080`, а в `docker-compose.yml`
   раскомментируй `extra_hosts: host-gateway`. Альтернатива — свой мирор в `TELEGRAM_API_BASE`.
   Проверка: `docker compose logs bot | grep -E "Бот:|getMe"` — должно быть `Бот: @username`.
4. **Порт занят.** Если 5000 на сервере уже кем-то занят — поменяй левую часть в `ports:`
   (`"8080:5000"`), внутри контейнера порт всегда 5000.
5. **Время.** В контейнере `TZ=Europe/Moscow` (задан в `docker-compose.yml` и в Dockerfile).
   Если поменяешь — от него зависят напоминания 48/24/12 часов: дедлайны парсятся в локальном
   времени.

### lazydocker (мышкой по контейнеру, если нравится такой стиль)

```bash
# Linux
curl https://raw.githubusercontent.com/jesseduffield/lazydocker/master/scripts/install_update_linux.sh | bash
# Windows (у тебя есть winget)
winget install jesseduffield.lazydocker
```

Запускается из папки проекта командой `lazydocker`: слева контейнеры, `l` — логи,
`r` — рестарт, `d` — удалить, `enter` — меню. Ничего дополнительно настраивать не надо, он
подхватывает тот же `docker-compose.yml`.

### Что проверено, а что нет

* ✅ `docker compose config` — файл валиден, переменные из `.env` подставляются, том и порт
  на месте.
* ✅ `tools/healthcheck.py` — проверен живьём против работающей админки (`HTTP 200`, код 0).
* ✅ То, что приложение живёт одним процессом и пишет базу в `instance/` — проверено на живом
  прогоне (16 работ, уведомления, кнопки).
* ❌ **Сам образ не собирался и не запускался**: на машине автора Docker Desktop не стартует
  (демон недоступен), а на сервер доступа не было. Первый `docker compose up -d --build` — за
  тобой: если что-то отвалится, это почти наверняка пункт про права на `./instance` или прокси.

---

## 4. Telegram из РФ: прокси

С российского сервера `api.telegram.org` часто недоступен. В `app/bot/telegram_bot.py` все
запросы идут через `requests` и уважают два параметра из `.env`:

* `TELEGRAM_PROXY` — `socks5h://127.0.0.1:1080` или `http://user:pass@host:port`;
* `TELEGRAM_API_BASE` — свой мирор/реверс-прокси Bot API вместо `https://api.telegram.org`.

### Вариант A: tg-ws-proxy (SOCKS5 на localhost)

```bash
sudo apt install -y python3-pip git
sudo -u timeline git clone https://github.com/Flowseal/tg-ws-proxy /opt/tg-ws-proxy
cd /opt/tg-ws-proxy && sudo -u timeline python3 -m venv venv
sudo -u timeline venv/bin/pip install -e .
sudo -u timeline venv/bin/tg-ws-proxy --host 127.0.0.1 --port 1080      # проверка
# дальше — юнит из deploy/tg-ws-proxy.service (поправь ExecStart на свой venv/bin/tg-ws-proxy)
sudo cp deploy/tg-ws-proxy.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now tg-ws-proxy
```

В `.env`: `TELEGRAM_PROXY=socks5h://127.0.0.1:1080`, затем `sudo systemctl restart timeline-notifier`.

**Важно:** свежие версии tg-ws-proxy перехватывают только IP дата-центров Telegram, а не
`api.telegram.org`, поэтому HTTP Bot API через него может не ходить. Для Bot API в обсуждениях
советуют [релиз 1.3.0](https://github.com/Flowseal/tg-ws-proxy/discussions/616). Проверка:

```bash
curl -s --max-time 10 -x socks5h://127.0.0.1:1080 "https://api.telegram.org/bot<TOKEN>/getMe"
```

Если видишь `{"ok":true,...}` — прокси работает.

### Вариант B: свой мирор Bot API

Поднимаешь Cloudflare Worker или nginx на зарубежном VPS, который проксирует на
`https://api.telegram.org`, и ставишь `TELEGRAM_API_BASE=https://tg-api.твой-домен`.

### Вариант C: обычный внешний прокси

`TELEGRAM_PROXY=http://user:pass@1.2.3.4:3128` (нужен пакет `PySocks` — он в `requirements.txt`).

### Диагностика канала

```bash
curl -s --max-time 10 "https://api.telegram.org/bot<TOKEN>/getMe"                        # напрямую
curl -s --max-time 10 -x socks5h://127.0.0.1:1080 "https://api.telegram.org/bot<TOKEN>/getMe"  # через прокси
journalctl -u timeline-notifier -n 50 | grep -i -E "getMe|Telegram|proxy"
```

---

## 5. Справочник `.env`

| Переменная | По умолчанию | Зачем |
|---|---|---|
| `SECRET_KEY` | — | ключ Flask (подпись/сессии). Сгенерировать один раз |
| `CRED_KEY` | `SECRET_KEY` | ключ шифрования паролей ЭИОС. **Не менять** после запуска |
| `ADMIN_CHAT_ID` | пусто | твой telegram-id: включает у бота команды `/admin`, `/broadcast` |
| `ADMIN_USER` / `ADMIN_PASSWORD` | `admin` / пусто | Basic Auth админки. Пустой пароль = админка открыта (только локалка!) |
| `TELEGRAM_PROXY` | пусто | SOCKS5/HTTP прокси для Bot API |
| `TELEGRAM_API_BASE` | `https://api.telegram.org` | мирор Bot API |
| `TELEGRAM_TIMEOUT` | `20` | таймаут обычных запросов к Telegram, сек |
| `TELEGRAM_POLL_TIMEOUT` | `25` | long-polling getUpdates, сек |
| `DEFAULT_CHECK_INTERVAL` | `300` | интервал проверки для новых юзеров, сек (5 мин) |
| `SCHEDULER_TICK` | `60` | как часто планировщик смотрит, кому пора, сек |
| `CHECK_WORKERS` | `4` | сколько логинов в Timeline одновременно. Больше 8 не ставь — сайт не любит |
| `MIN_CHECK_GAP` | `60` | не логиниться в Timeline чаще, чем раз в N секунд на человека (защита от кнопкотыканья) |
| `LOGIN_ATTEMPTS` | `3` | сколько раз пробовать вход при обрыве связи (сайт УрГПУ иногда рвёт соединение) |
| `LOGIN_RETRY_DELAY` | `2` | пауза между попытками входа, сек |
| `WEB_HOST` / `WEB_PORT` | `0.0.0.0` / `5000` | где слушает админка (`127.0.0.1` за nginx) |
| `DATABASE_URL` | `sqlite:///instance/timeline.db` | строка подключения SQLAlchemy |
| `TIMELINE_*_URL` | адреса УрГПУ | если сайт переедет |
| `TIMELINE_VERIFY_SSL` | `false` | проверка сертификата сайта |
| `TIMELINE_DEBUG_DUMP` | `false` | сохранять сырые HTML/JSON в `instance/debug/` для отладки |

Bot Token в `.env` не нужен — он хранится в БД и задаётся из админки.

---

## 6. Как пользоваться

### Студент (в Telegram)

Меню — **постоянные кнопки внизу экрана** (reply-клавиатура), никаких простыней текста:

```
[ 📊 Статус ]   [ 💀 Просрочки ]
[ 🔄 Проверить ] [ ⚙️ Профиль ]
[ ❓ Помощь ]
```

| Кнопка / команда | Что делает |
|---|---|
| `/start` | регистрация: логин → пароль (сообщение с паролем удаляется) |
| 📊 Статус · `/status` | работы **по 4 на страницу** с листалкой «⬅️ Назад / Вперёд ➡️», переключателем сортировки и счётчиком `стр. 1/4`. Просроченные помечены 💀 |
| 💀 Просрочки · `/overdue` | только просроченные и неотправленные |
| 🔄 Проверить · `/check` | зайти в Timeline прямо сейчас (одно сообщение, которое обновляется на месте) |
| ⚙️ Профиль | логин, уведомления, интервал; кнопки «сменить данные», «вкл/выкл уведомления», «удалить» |
| ❓ Помощь · `/help` | справка |
| `/interval 15` | как часто проверять именно его, в минутах (минимум 5) |
| `/login` | сменить логин/пароль |
| `/unlink` | удалить свои данные и работы из бота |
| `/cancel` | отменить ввод |

Свои работы студент видит только свои: бот фильтрует всё по `user_id`.

**Бот не паникует зря.** «Пароль неверный» он напишет только если сайт *явно* вернул форму
логина (реальный отказ). Обрывы связи, таймауты и поломки парсера — это отдельные причины:
бот сначала трижды повторит попытку сам, а человека побеспокоит только после 3 неудач подряд,
и текст будет честный («Timeline не отвечает, с данными всё в порядке»). Про проблемы сайта
он отдельно пишет админу (`ADMIN_CHAT_ID`), а сама причина видна в админке в поле «ошибка».

**Список работ — постранично.** По 4 работы на страницу, нумерация сквозная, внизу кнопки
«⬅️ Назад / Вперёд ➡️» и переключатель сортировки: «🆕 Новые сверху» (по дате появления работы,
по умолчанию) ↔ «⏰ По срокам» (сначала то, что горит). Размер страницы — константа
`STATUS_PAGE_SIZE` в [app/bot/telegram_bot.py](app/bot/telegram_bot.py).

**Шутки — в одном файле.** Все подписи-подъёбки живут в [app/bot/jokes.py](app/bot/jokes.py):
158 штук в 22 разделах (статус, просрочки, профиль, справка, уведомления о новых работах,
напоминания, ошибки, админка и т.д.). Бот берёт случайную из нужного раздела, а на страницах
статуса — детерминированно по номеру страницы, чтобы текст не прыгал при листании. Хочешь свои
приколы — просто правь списки там, логику трогать не надо.

### Админ

* Веб: `http://<сервер>:5000/` — сводка, список пользователей, карточка пользователя
  (вкл/выкл, свой интервал, «Проверить сейчас», «Тест Telegram», «Удалить»), кнопки
  «Проверить всех» и «Перезапустить бота».
* В Telegram (если задан `ADMIN_CHAT_ID`): `/admin` — сводка, `/broadcast текст` — рассылка
  всем активным пользователям.

---

## 7. База данных и бэкапы

SQLite-файл: `instance/timeline.db`. Три таблицы:

| Таблица | Поля (главные) |
|---|---|
| `setting` | `telegram_bot_token`, `check_interval`, `last_check_at` — одна строка, общие настройки |
| `bot_user` | `chat_id` (уникальный), `timeline_login`, `password_enc`, `is_registered`, `is_active`, `state`, `pending_login`, `notify_enabled`, `check_interval` (0 = общий), `last_check_at`, `last_error` |
| `user_work_item` | `user_id`, `external_id`, `discipline`, `deadline`, `status`, флаги `reminder_48/24/12_sent`, `overdue_sent`, `notified_new`; уникально `(user_id, external_id)` |

Полезные запросы:

```bash
sqlite3 instance/timeline.db "select chat_id, timeline_login, is_active, last_error from bot_user;"
sqlite3 instance/timeline.db "select count(*) from user_work_item;"
```

Бэкап и восстановление (бота на паузу, потом обратно):

```bash
sudo systemctl stop timeline-notifier
sudo cp instance/timeline.db ~/timeline-$(date +%F).db
sudo systemctl start timeline-notifier
```

Сброс/пересоздание (все пользователи и работы удалятся — они заново напишут боту):

```bash
sudo systemctl stop timeline-notifier
sudo rm -f instance/timeline.db
sudo systemctl start timeline-notifier      # таблицы создадутся сами
```

**Схема меняется только пересозданием.** Миграций (alembic) в проекте нет: изменил
`app/models.py` → останови сервис, удали `instance/timeline.db`, запусти. Пользователям придётся
заново прислать логин/пароль.

---

## 8. Диагностика: «не работает»

Сначала всегда:

```bash
systemctl status timeline-notifier
journalctl -u timeline-notifier -n 100 --no-pager
```

| Симптом | Причина | Что делать |
|---|---|---|
| Бот молчит на `/start` | неверный токен, нет сети/прокси, поллер не стартовал | в логе `Не удалось получить getMe` → прокси ([раздел 4](#4-telegram-из-рф-прокси)); `Bot token не задан` → вписать токен в админке |
| **Уведомления приходят, а команды боту не работают** | **тот же токен опрашивает второй процесс (старая версия, забытый запуск на другом порту)** — Telegram отдаёт апдейты только одному | найти и убить лишний процесс: `ps aux \| grep run.py`, `ss -ltnp \| grep 5000`, `Get-NetTCPConnection -LocalPort 5000`; если убить не выходит — отозвать токен у @BotFather и вписать новый |
| `409 Conflict: terminated by other getUpdates` | то же самое: больше одного поллера на один токен | `systemctl list-units 'timeline*'`, убить лишний; **не** запускать gunicorn в несколько воркеров |
| В логе `Авторизация не удалась: chat_id=..., причина: ...` | `rejected` — сайт реально не принял пароль; `network` — обрыв связи/таймаут; `site_changed` — поменялась страница входа; `flow` — вход прошёл, но рабочая страница не открылась | `rejected` → человек жмёт `/login`; остальное — проблема сайта, смотри `instance/debug/` и [docs/parser-notes.md](docs/parser-notes.md) |
| **Бот написал «Timeline не принял твой логин/пароль», хотя пароль верный** | раньше сюда попадали любые сбои; теперь так пишется только при явном отказе сайта. Если всё же пришло — смотри `причина` в логе: скорее всего `rejected` из-за реального отзыва сессии/смены пароля | проверить вход руками (сниппет в [docs/parser-notes.md](docs/parser-notes.md)); при массовых ложных срабатываниях поднять `LOGIN_ATTEMPTS` |
| `ConnectionResetError(10054)` / `Read timed out` в логе | сайт УрГПУ рвёт соединение (нагрузка/троттлинг) | ничего не делать: бот сам повторит (`LOGIN_ATTEMPTS`) и напишет человеку только после 3 неудач подряд; можно увеличить `DEFAULT_CHECK_INTERVAL` |
| Проверки не идут вообще | все выключены / не зарегистрированы | в админке посмотреть `Активных`; интервал в карточке пользователя |
| Уведомления приходят дважды | два процесса, либо пересоздание работы | п. про 409 Conflict; проверить `journalctl` на два `Планировщик запущен` |
| Время напоминаний съехало на 3 часа | сервер в UTC | `sudo timedatectl set-timezone Europe/Moscow` + `systemctl restart timeline-notifier` |
| Админка просит пароль, который не подходит | `ADMIN_PASSWORD` из `.env` не подхватился | правил `.env` → `systemctl restart timeline-notifier`; пустой `ADMIN_PASSWORD` = без пароля |
| `database is locked` | несколько процессов пишут в SQLite | один процесс; проверить, что нет второго запуска |
| `Не удалось расшифровать пароль` | сменился/потерялся `CRED_KEY` | вернуть прежний ключ. Пользователю бот сам напишет «Пришли данные заново: /login», в админке в карточке будет эта же ошибка; проверка Timeline при этом не дёргается каждую минуту |
| Пропали работы у всех после обновления | менялась схема БД | нормально: следующий цикл проверки всё подтянет, если пользователи на месте |

Проверить вход вручную и разобрать сайт — пошагово в
[docs/parser-notes.md](docs/parser-notes.md) (там же включение дампов `TIMELINE_DEBUG_DUMP=true`).

---

## 9. Как дорабатывать код

Тесты (без сети, на временной БД, реальные аккаунты не трогаются):

```bash
venv/bin/python tests/smoke_test.py     # ожидаемый вывод: OK: все проверки пройдены
```

Тест сам создаёт `instance/smoke-test.db`, гоняет онбординг, изоляцию данных между двумя
пользователями, напоминания, `check_user()` и страницы админки, а потом удаляет файл. Все
обращения к Timeline и Telegram подменены заглушками (`FakeAuth`, `FakeScraper`, `DummySender`),
поэтому прогон можно делать на сервере и в CI, ничего не сломав.

### Добавить команду бота

1. `app/bot/telegram_bot.py`, метод `_handle_message()` — добавить ветку `elif low.startswith('/моякоманда')`.
2. Если команда нужна из меню — добавь кнопку в `_menu_keyboard()` и ветку в `_handle_callback()`.
3. Новую текстовую команду опиши в `HELP_TEXT`.

### Поменять тексты уведомлений

Функции `notify_new_works`, `notify_status_changes`, `notify_deadline_reminders`, `notify_overdue`
в конце `app/bot/telegram_bot.py`. Тексты меню/онбординга — методы `_cmd_*`, `_ask_*`,
`_build_*_text` там же.

### Поменять пороги напоминаний

`app/scheduler/jobs.py` → `_check_deadline_reminders()` (логика 48/24/12/просрочка) и
`notify_deadline_reminders()` (подписи). Добавляешь новый порог — не забудь поле-флаг в
`WorkItem` (`app/models.py`) и пересоздать БД.

### Как устроен планировщик (если что-то не проверяется)

* `start_scheduler()` — один job `check_all_users` с `trigger='interval', seconds=SCHEDULER_TICK`.
* `_due_users()` — фильтр «кому пора»: `last_check_at + (check_interval or глобальный) <= now`.
* `check_all_users()` — `ThreadPoolExecutor(max_workers=CHECK_WORKERS)`, по одному потоку на юзера,
  каждая ошибка ловится и пишется в лог (`Проверка пользователя N упала`).
* `check_user()` — вся работа за одного человека: логин, `fetch_works`, diff, уведомления,
  `last_check_at` и `last_error`. Ручная проверка из бота/админки вызывает её же.

### Логи

`logging.basicConfig` в `app/__init__.py` (формат `ЧЧ:ММ:СС [LEVEL] модуль: сообщение`).
Хочешь больше деталей — включи `TIMELINE_DEBUG_DUMP=true` и смотри `instance/debug/`.

---

## 10. Ограничения и куда развивать

* **Один процесс.** Планировщик и polling живут внутри `run.py`; gunicorn с `-w 4` породит
  четыре поллера и дубли уведомлений. Если нужна «взрослая» эксплуатация — вынести
  планировщик в отдельный процесс/сервис и оставить веб отдельно.
* **SQLite.** Для пары сотен студентов хватает с запасом. При росте — Postgres и
  `DATABASE_URL=postgresql+psycopg://...` (код менять не придётся, только зависимость).
* **Пароли ЭИОС в БД.** Они шифруются Fernet, но ключ лежит рядом, в `.env` — то есть защита
  от «утащили файл БД», а не от «утащили сервер». Альтернатива, если заморочиться: не хранить
  пароли, а просить у пользователя свежий вход раз в N дней.
* **Flask dev-сервер** под systemd. Для админки на 1–2 человека нормально; при желании —
  `waitress-serve --threads 8 --port 5000 run:app` **плюс** отдельный запуск планировщика и бота
  (см. первый пункт).
* Идеи на потом: массовая выгрузка статистики в CSV, экспорт «кто не сдал» для старосты,
  веб-хук вместо long-polling (экономит трафик), второй язык интерфейса, напоминания о
  приближении к концу семестра.
