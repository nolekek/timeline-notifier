# Как устроен парсер Timeline (и как его чинить)

Всё, что связано с сайтом УрГПУ, живёт в двух файлах:

| Файл | Ответственность |
|---|---|
| `app/parser/auth.py` | вход в ЭИОС по логину/паролю, хранение cookie-сессии |
| `app/parser/scraper.py` | получение списка работ (`SendGrid`) и разбор полей |

Бот ничего не знает про HTML — он получает уже нормализованный список словарей
`{external_id, discipline, deadline, status, is_overdue}`.

---

## 1. Вход (`auth.py`)

Схема входа — это OAuth-цепочка с редиректами:

1. `GET https://eios.rsvpu.ru/Account/Login` — уходит по редиректам, в итоге оказывается на
   `https://timeline.rsvpu.ru/Account/OAuthLogin?...`. Именно этот URL (`resp.url`) — база для
   дальнейших шагов; **не** хардкодим `eios`.
2. Со страницы берётся **первая** форма и собирается payload:
   * `hidden` → значение как есть (там `__RequestVerificationToken`);
   * `password` → пароль пользователя;
   * `text` / `email` → логин (поле называется `Login`);
   * остальные типы игнорируются.
3. `POST` на `urljoin(текущий_url, form.action)` с заголовками `Referer`, `Origin`,
   `Content-Type: application/x-www-form-urlencoded` — без `Referer`/`Origin` ASP.NET
   отбивает запрос.
4. Проверка успеха: `GET {base}/StudentWork/Send`. Если в итоговом URL нет `Account/Login`
   и статус 200 — вход удался.

Если сайт переехал или поменял поля формы — правится только этот файл.

### Причины неудачного входа (`TimelineAuth.reason`)

`login()` возвращает `True/False`, а причину отказа кладёт в `self.reason` — от неё зависит,
что бот скажет человеку и стоит ли повторять:

| `reason` | Когда бывает | Что делает бот |
|---|---|---|
| `rejected` | после POST сайт вернул форму логина | говорит человеку «Timeline не принял логин/пароль», просит `/login` (проверка сайтом пройдена, это точно отказ) |
| `network` | `ConnectionResetError`, таймаут, 5xx — сайт рвёт соединение | **молчит** и повторяет попытку (`LOGIN_ATTEMPTS`, пауза `LOGIN_RETRY_DELAY`); если 3 проверки подряд — пишет «Timeline не отвечает, с данными всё в порядке» и уведомляет админа |
| `site_changed` | на странице входа не нашлась форма | повторять бессмысленно: пишет админу, человеку — после 3 неудач подряд |
| `flow` | POST прошёл (нас пустили), но `StudentWork/Send` снова увёл на логин | как `site_changed`: проблема сессии/флоу, а не пароля |

Именно из-за отсутствия такого разделения в первой версии бот писал «смени пароль» при
банальном обрыве связи (`ConnectionResetError(10054)`) — теперь так не бывает.

## 2. Список работ (`scraper.py`)

1. `GET {base}/StudentWork/Send` — из `<select name="SemesterId">` вытаскивается id выбранного
   семестра (регулярка по `option ... selected ... value="N"`).
2. `GET {base}/StudentWork/SendGrid` — тот же эндпоинт, что использует сам сайт для таблицы
   (jqGrid). Параметры: `semesterId`, `rows=100`, `page=1`, `sidx=Deadline`, `sord=asc`,
   заголовок `X-Requested-With: XMLHttpRequest`. Ответ — JSON: `{"Records": N, "Rows": [...]}`.
3. Из каждой строки берём:
   * `Id` → `external_id` (должен быть числом, иначе строка пропускается);
   * `EduModuleName` → `discipline` (пусто → строка пропускается);
   * `Deadline` → `"/Date(1788721199000)/"` — миллисекунды Unix, парсим в `ДД.ММ.ГГГГ ЧЧ:ММ`
     **в локальном времени сервера** (поэтому сервер должен быть в Europe/Moscow);
   * статус вычисляется по полям строки:
     `PointAccepted == true` → `Зачтена`, `WorkChecked == true` → `Проверена`,
     `WorkStatusId > 0` → `Отправлена`, иначе → `Не отправлена`.

Статусы, по которым ничего не напоминаем: `SKIP_STATUSES = {'Отправлена', 'Проверена', 'Зачтена'}`
(в `scraper.py`). Статусы, по которым напоминаем: `NOT_SENT_STATUSES = ('', 'Не отправлена')`
(в `scheduler/jobs.py`).

## 3. Образец реального ответа

`docs/sample_sendgrid_response.json` — сохранённый ответ `SendGrid` (16 работ, сентябрь 2026).
По нему видно структуру строки: `Id`, `EduModuleName`, `Deadline`, `WorkStatusId`,
`WorkChecked`, `PointAccepted`. Если сайт что-то поменяет, сравнивайте свежий ответ с этим.

### Что проверено на живом сайте (сентябрь 2026)

Контрольный прогон реальными данными (логин студента, реальный сервер):

```
Форма логина: https://timeline.rsvpu.ru/Account/OAuthLogin?returnUrl=%2FOAuth20%2FAuth...
POST -> https://timeline.rsvpu.ru/Account/OAuthLogin?returnUrl=...
После POST: https://eios.rsvpu.ru/Profile          <- успешный вход
Авторизация прошла успешно
Используется semesterId=30                          <- определился автоматически
Всего записей в ответе: 16 (Records=16)             <- SendGrid отдал работы
Обработано работ: 16
```

Если однажды эти строки начнут отличаться (например `semesterId` не определится или
`Обработано работ: 0`) — сайт поменялся, и по этой таблице логов сразу видно, на каком шаге.

## 4. Как получить свежие данные для отладки

Включить дампы в `.env`:

```env
TIMELINE_DEBUG_DUMP=true
```

и перезапустить сервис. Тогда:

* `instance/debug/login-form-*.html` — страница формы входа;
* `instance/debug/login-after-post-*.html` — что сайт ответил на POST;
* `instance/debug/login-check-failed-*.html` — страница `StudentWork/Send`, если вход не подтвердился;
* `instance/debug/sendgrid-*.json` — сырой ответ со списком работ.

Дампы копятся, чистить: `rm -f instance/debug/*`. Потом выключить флаг обратно.

## 5. Проверить вход руками (без бота)

```bash
cd /opt/timeline_notifier && venv/bin/python - <<'PY'
from app import create_app
from app.parser.auth import build_auth
app = create_app()
with app.app_context():
    ok = build_auth(app).login('ivanov.i.i', 'пароль')
    print('вход:', ok)
PY
```

Проверить только разбор списка (после успешного входа):

```bash
venv/bin/python - <<'PY'
from app import create_app
from app.parser.auth import build_auth
from app.parser.scraper import TimelineScraper
app = create_app()
with app.app_context():
    auth = build_auth(app)
    auth.login('ivanov.i.i', 'пароль')
    works = TimelineScraper(auth, app.config['TIMELINE_WORKS_URL'],
                            app.config['TIMELINE_GRID_URL'],
                            debug_dir=app.config['TIMELINE_DEBUG_DIR']).fetch_works()
    for w in works:
        print(w)
PY
```

## 6. Типичные поломки

| Симптом в логах | Причина | Что делать |
|---|---|---|
| `Ошибка при логине (связь): ... ConnectionResetError(10054)` | сайт рвёт соединение (нагрузка/троттлинг), пароль тут ни при чём | ничего: бот повторит сам; при частых сбоях поднять `LOGIN_ATTEMPTS`/`DEFAULT_CHECK_INTERVAL` |
| `Форма логина не найдена на странице` | изменилась страница входа | открыть `instance/debug/login-form-*.html`, поправить разбор формы в `auth.py` |
| `Авторизация не подтверждена. URL: ...` | не приняли токен/заголовки, капча, неверный пароль | посмотреть `login-check-failed-*.html`; проверить руками (п. 5) |
| `Не удалось определить semesterId` | поменялся селект семестра | поправить регулярку `_detect_semester_id` |
| `SendGrid вернул не JSON` | сессия протухла или сайт отдаёт HTML | включить дампы, посмотреть `sendgrid-*.json` |
| `Обработано работ: 0`, а работы есть | изменились имена полей | сравнить с `docs/sample_sendgrid_response.json`, поправить `fetch_works` |
| Дедлайны сдвинуты на 3 часа | сервер в UTC | `sudo timedatectl set-timezone Europe/Moscow` |
