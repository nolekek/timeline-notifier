"""Быстрая проверка логики без сети.

Запуск: python tests/smoke_test.py     (ожидаемый вывод: OK: все проверки пройдены)

Что проверяем: онбординг через бота, изоляцию данных между пользователями, напоминания,
core-цикл планировщика (включая честный разбор причин неудачного входа), нижнее меню,
страницы админки. Сеть не используется: Timeline и Telegram подменены заглушками,
БД — временная (instance/smoke-test.db, удаляется на выходе).
"""
import atexit
import base64
import os
import random
import sys
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

# Шутки бота выбираются случайно — фиксируем сид, чтобы прогон был воспроизводимым
random.seed(20260930)

_here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _here)

# Временная БД внутри instance/ (в git не попадает, чистится на выходе)
_tmp_db = os.path.join(_here, 'instance', 'smoke-test.db')
os.makedirs(os.path.dirname(_tmp_db), exist_ok=True)


def _cleanup():
    """Закрыть соединения SQLite и удалить временную БД (на Windows файл залочен)."""
    try:
        from app.models import db
        with app.app_context():
            db.session.remove()
            db.engine.dispose()
    except Exception:
        pass
    try:
        os.remove(_tmp_db)
    except OSError:
        pass


atexit.register(_cleanup)
if os.path.exists(_tmp_db):
    try:
        os.remove(_tmp_db)
    except OSError:
        pass
os.environ['DATABASE_URL'] = f'sqlite:///{_tmp_db}'
os.environ.setdefault('SECRET_KEY', 'smoke-test-key')
os.environ.setdefault('CRED_KEY', 'smoke-test-key')
os.environ['ADMIN_CHAT_ID'] = ''


from app import create_app  # noqa: E402
import app.bot.telegram_bot as bot_mod  # noqa: E402
import app.parser.auth as auth_mod  # noqa: E402
import app.parser.schedule as sched_mod  # noqa: E402
import app.scheduler.jobs as jobs_mod  # noqa: E402
from app.bot.jokes import JOKES  # noqa: E402
from app.bot.telegram_bot import BTN_CHECK, BTN_SCHEDULE, BTN_STATUS, SORT_DEADLINE, TelegramBot  # noqa: E402
from app.models import db, User, WorkItem  # noqa: E402
from app.parser.auth import REASON_NETWORK, REASON_REJECTED  # noqa: E402
from app.parser.schedule import format_day_schedule, get_schedule_view, parse_schedule_html  # noqa: E402
from app.scheduler.jobs import _check_deadline_reminders, _due_daily  # noqa: E402
from app.security import decrypt, encrypt  # noqa: E402

app = create_app()

CHAT_A = '111'
CHAT_B = '222'
soon = (datetime.now() + timedelta(hours=20)).strftime('%d.%m.%Y %H:%M')
past = (datetime.now() - timedelta(hours=3)).strftime('%d.%m.%Y %H:%M')

SENT = []


def fake_api(self, method, payload=None, timeout=None):
    """Вместо Telegram API — записываем вызовы."""
    SENT.append((method, payload or {}))
    return {'message_id': len(SENT)}


class SyncThread:
    """Потоки внутри бота выполняем синхронно — так тест детерминированный."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, **kw):
        self._target, self._args, self._kwargs = target, args, kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)

    def is_alive(self):
        return False


class DummySender:
    def __init__(self, outbox):
        self.outbox = outbox

    def send_message(self, chat_id, text, reply_markup=None):
        self.outbox.append((str(chat_id), text))
        return True


TelegramBot._api = fake_api
bot_mod.threading = SimpleNamespace(Thread=SyncThread, Event=threading.Event,
                                    Lock=threading.Lock)


def msg(chat_id, text, message_id=1, first_name='Тест'):
    return {
        'message_id': message_id,
        'chat': {'id': int(chat_id), 'type': 'private'},
        'from': {'id': int(chat_id), 'first_name': first_name},
        'text': text,
    }


def texts(method='sendMessage'):
    return [p.get('text', '') for m, p in SENT if m == method]


def reset_outbox():
    SENT.clear()


with app.app_context():
    bot = TelegramBot('0:test', app)

    # 0. меню — reply-клавиатура внизу экрана
    kb = bot._main_keyboard()
    assert 'keyboard' in kb and kb['is_persistent'] and kb['resize_keyboard'], kb
    assert [b['text'] for row in kb['keyboard'] for b in row][:4] == [
        BTN_STATUS, '💀 Просрочки', BTN_CHECK, '⚙️ Профиль']
    assert bot._norm(BTN_CHECK) == 'проверить'

    # 1. новый пользователь -> просим логин
    bot._handle_message(msg(CHAT_A, '/start'))
    user_a = User.query.filter_by(chat_id=CHAT_A).first()
    assert user_a is not None and user_a.state == 'login', user_a
    assert any('логин' in t.lower() for t in texts()), texts()

    # 2. логин -> просим пароль
    reset_outbox()
    bot._handle_message(msg(CHAT_A, 'test.student'))
    assert user_a.state == 'password' and user_a.pending_login == 'test.student'

    # 3. пароль -> сообщение удаляется, пара проверяется, регистрация завершается
    reset_outbox()
    auth_mod.try_login = lambda app_, login, password: (True, None)
    real_check_user = jobs_mod.check_user
    jobs_mod.check_user = lambda *a, **k: 'ok'
    bot._handle_message(msg(CHAT_A, 'secret-pass', message_id=42))
    assert any(m == 'deleteMessage' for m, _ in SENT), 'пароль не удалён из чата'
    assert any(m == 'editMessageText' for m, _ in SENT), 'статус не отредактирован на месте'
    jobs_mod.check_user = real_check_user

    db.session.expire_all()  # _finish_registration работала в своём app context
    user_a = db.session.get(User, user_a.id)
    assert user_a.is_registered and decrypt(user_a.password_enc) == 'secret-pass'
    assert user_a.state == 'ready'

    # 4. второй пользователь — свои работы, изоляция данных
    bot._handle_message(msg(CHAT_B, '/start'))
    user_b = User.query.filter_by(chat_id=CHAT_B).first()
    user_b.timeline_login = 'other.student'
    user_b.is_registered = True
    user_b.state = 'ready'
    db.session.add_all([
        WorkItem(user_id=user_a.id, external_id='1', discipline='Матан A',
                 deadline=soon, status='Не отправлена'),
        WorkItem(user_id=user_b.id, external_id='1', discipline='Физика B',
                 deadline='01.01.2030 10:00', status='Не отправлена'),
    ])
    db.session.commit()

    text_a = bot._build_status_text(user_a)
    text_b = bot._build_status_text(user_b)
    assert 'Матан A' in text_a and 'Физика B' not in text_a
    assert 'Физика B' in text_b and 'Матан A' not in text_b

    # 5. напоминания считаются по работам одного пользователя
    groups, overdue = _check_deadline_reminders(
        WorkItem.query.filter_by(user_id=user_a.id).all())
    assert sum(len(v) for v in groups.values()) + len(overdue) == 1

    # 6. незарегистрированный получает просьбу о логине, а не статус
    reset_outbox()
    bot._handle_message(msg('333', '/status'))
    assert any('логин' in t.lower() for t in texts()), texts()

    # 7. кнопки нижнего меню работают как команды
    reset_outbox()
    bot._handle_message(msg(CHAT_A, BTN_STATUS))
    assert any('Матан A' in t for t in texts()), texts()

    reset_outbox()
    bot._handle_message(msg(CHAT_A, '/interval 15'))
    assert user_a.check_interval == 900
    assert 'test.student' in bot._build_profile_text(user_a)

    # 7.1 страницы статуса: 4 работы на страницу, новые сверху, листалка и сортировка
    user_p = User(chat_id='555', timeline_login='p.student', password_enc=encrypt('x'),
                  is_registered=True, state='ready')
    db.session.add(user_p)
    db.session.commit()
    base = datetime.now(timezone.utc).replace(tzinfo=None)
    for i in range(6):
        db.session.add(WorkItem(user_id=user_p.id, external_id=f'p{i}',
                                discipline=f'Работа {i}', deadline=soon,
                                status='Не отправлена',
                                first_seen_at=base - timedelta(days=i)))
    db.session.commit()

    page1, kb1 = bot._build_status_page(user_p, 1)
    assert 'Работа 0' in page1 and 'Работа 3' in page1 and 'Работа 4' not in page1, page1
    assert 'стр. 1/2' in page1 and '▰' in page1  # счётчик страниц и тупая полоска
    assert any(j in page1 for j in JOKES['status']), page1
    assert [b['callback_data'] for row in kb1['inline_keyboard'] for b in row] == [
        'wv|p0|new|1', 'wv|p1|new|1', 'wv|p2|new|1', 'wv|p3|new|1',
        'noop', 'st|new|2', 'st|dl|1', 'st_compact'], kb1

    page2, kb2 = bot._build_status_page(user_p, 2)
    assert 'Работа 4' in page2 and 'Работа 0' not in page2, page2
    assert any(j in page2 for j in JOKES['status_last']), page2
    assert [b['callback_data'] for row in kb2['inline_keyboard'] for b in row] == [
        'wv|p4|new|2', 'wv|p5|new|2',
        'st|new|1', 'noop', 'st|dl|1', 'st_compact'], kb2

    by_deadline, _ = bot._build_status_page(user_p, 1, SORT_DEADLINE)
    assert 'по срокам' in by_deadline

    # 7.2 приколы есть во всех разделах и их реально много
    assert all(len(pool) >= 5 for pool in JOKES.values()), \
        {k: len(v) for k, v in JOKES.items() if len(v) < 5}
    assert sum(len(pool) for pool in JOKES.values()) >= 5 * len(JOKES)
    prof = bot._build_profile_text(user_p)
    assert any(j in prof for j in JOKES['profile']), prof
    ov = bot._build_overdue_text(user_p)
    assert any(j in ov for j in JOKES['overdue_clean']), ov
    help_text = bot._help_text()
    assert any(j in help_text for j in JOKES['help']), help_text
    adm = bot._admin_text()
    assert any(j in adm for j in JOKES['admin']), (adm, JOKES['admin'])

    reset_outbox()
    bot._handle_callback({
        'id': 'cb-page', 'data': 'st|new|2',
        'message': {'chat': {'id': 555, 'type': 'private'}, 'message_id': 7},
        'from': {'id': 555, 'first_name': 'P'},
    })
    assert any('Работа 4' in t for t in texts('editMessageText')), texts('editMessageText')

    # 7.3 парсинг и команды динамического расписания (uspu.ru)
    sample_rasp_html = """
    <div class="rasp-item">
        <span class="rasp-day">26 октября</span>
        <div class="rasp-descr">
            <div class="rasp-week">Пн</div>
            <div class="rasp-item-inner">
                <div class="rasp-para">
                    <span class="para-time">08:30 - 10:00</span>
                    <div class="rasp-desc">
                        <p>Математический анализ (Лекции)<br>
                            <span>Ауд. 201 УК М11<br>
                            Преподаватель: Иванов Иван Иванович<br>
                            Группа: МКТ-2531<br>
                            подгруппа 1</span></p>
                    </div>
                </div>
            </div>
        </div>
    </div>
    """
    days = parse_schedule_html(sample_rasp_html, reference_date=datetime(2026, 10, 24).date())
    assert len(days) == 1
    assert days[0]['lessons'][0]['subject'] == 'Математический анализ (Лекции)'
    assert days[0]['lessons'][0]['time'] == '08:30 - 10:00'
    assert 'Иванов' in days[0]['lessons'][0]['teacher']
    assert '201' in days[0]['lessons'][0]['classroom']
    assert 'подгруппа 1' in days[0]['lessons'][0]['subgroup']

    reset_outbox()
    bot._handle_message(msg(CHAT_A, BTN_SCHEDULE))
    assert any('расписание' in t.lower() for t in texts()), texts()

    # список групп подменяем — смоук-тест не должен ходить в сеть
    sched_mod.get_available_groups = lambda force_refresh=False: ['МКТ-2531', 'МКТ-2541']
    reset_outbox()
    bot._handle_message(msg(CHAT_A, '/group мкт-2531'))  # любой регистр
    assert user_a.group_name == 'МКТ-2531'
    assert any('МКТ-2531' in t for t in texts()), texts()

    # мусор отклоняется: формально невалидный — регуляркой, валидный, но
    # несуществующий — сверкой со списком групп сайта
    reset_outbox()
    bot._handle_message(msg(CHAT_A, '/group <script>alert(1)</script>'))
    assert any('1–50 символов' in t for t in texts()), texts()
    reset_outbox()
    bot._handle_message(msg(CHAT_A, '/group ААА-9999'))
    assert any('нет в расписании' in t for t in texts()), texts()
    assert user_a.group_name == 'МКТ-2531'  # не изменилась

    # 7.4 текст расписания экранирован: сырые <>& с сайта не ломают Telegram HTML
    evil_day = {'date': datetime.now().date(), 'day_title': '26 октября',
                'weekday_short': 'Пн',
                'lessons': [{'time': '08:30', 'subject': '<b>Инъекция</b> & Co',
                             'classroom': 'Ауд <1>', 'teacher': 'Иванов <И.И.>',
                             'subgroup': ''}]}
    rendered = format_day_schedule(evil_day, 'МКТ-2531&"<x>')
    assert '<b>Инъекция</b>' not in rendered and '&lt;b&gt;Инъекция&lt;/b&gt;' in rendered
    assert '&amp;' in rendered

    # 7.5 навигация по неделям реально сдвигает окно дат
    base_monday = sched_mod._week_window(datetime.now().date(), 0)[0]
    fake_days = [{'date': base_monday + timedelta(days=i), 'day_title': f'день {i}',
                  'weekday_short': 'Пн', 'lessons': []} for i in range(14)]
    orig_fetch = sched_mod.get_group_schedule
    sched_mod.get_group_schedule = lambda *a, **kw: fake_days
    try:
        week0 = get_schedule_view('МКТ-2531', mode='week', offset_weeks=0)
        week1 = get_schedule_view('МКТ-2531', mode='today', offset_weeks=1)
        assert base_monday.strftime('%d.%m') in week0
        assert (base_monday + timedelta(days=7)).strftime('%d.%m') in week1
        assert base_monday.strftime('%d.%m') not in week1
    finally:
        sched_mod.get_group_schedule = orig_fetch

    # 7.6 автоопределение группы из работ Timeline
    from app.parser.scraper import extract_group_from_works
    test_works = [
        {'discipline': 'Безопасность жизнедеятельности (Сарапульцева Л.А.) (МКТ-2531)'},
        {'discipline': 'Психология (Воробьева М.А.) (МКТ-2531)'},
    ]
    assert extract_group_from_works(test_works) == 'МКТ-2531'

    # 7.6 окно ежедневных уведомлений: с настроенного времени, один раз в день
    now10 = datetime.now().replace(hour=10, minute=30)
    assert _due_daily(now10, '08:30', None) is True          # время пришло
    assert _due_daily(now10, '10:31', None) is False         # ещё рано
    assert _due_daily(now10, '10:30', now10.replace(hour=9)) is False  # уже слали
    assert _due_daily(now10, '10:30', now10 - timedelta(days=1)) is True  # вчера не счёт
    assert _due_daily(now10, 'мусор', None, default_hour=10, default_minute=0) is True
    assert _due_daily(now10, '25:99', None) is True          # кривое время → дефолт 08:00

    # 7.7 ручная проверка (send=False) не взводит флаги напоминаний
    rem_user = User(chat_id='777', timeline_login='r.student', password_enc=encrypt('x'),
                    is_registered=True, is_active=True, state='ready')
    db.session.add(rem_user)
    db.session.commit()
    db.session.add(WorkItem(user_id=rem_user.id, external_id='r1',
                            discipline='Горящая работа', deadline=soon,
                            status='Не отправлена'))
    db.session.commit()
    rem_items = WorkItem.query.filter_by(user_id=rem_user.id).all()
    _check_deadline_reminders(rem_items, send=False)
    db.session.commit()
    db.session.expire_all()
    it = WorkItem.query.filter_by(user_id=rem_user.id).first()
    assert it.reminder_24_sent is False, 'ручная проверка съела напоминание'

    # 8. ручная проверка правит одно сообщение вместо трёх
    reset_outbox()
    jobs_mod.check_user = lambda *a, **k: 'ok'
    bot._handle_message(msg(CHAT_A, BTN_CHECK))
    jobs_mod.check_user = real_check_user
    assert len(texts('sendMessage')) == 1 and 'Захожу в Timeline' in texts('sendMessage')[0]
    assert texts('editMessageText'), 'результат проверки не пришёл правкой сообщения'

    # 9. отвязка данных
    bot._handle_message(msg(CHAT_A, '/unlink'))
    assert user_a.is_registered is False and user_a.password_enc is None
    assert WorkItem.query.filter_by(user_id=user_a.id).count() == 0

    # 10. причины неудачного входа разбираются честно
    OUTBOX = []
    user_c = User(chat_id='444', timeline_login='c.student',
                  password_enc=encrypt('pass-c'), is_registered=True,
                  is_active=True, state='ready')
    db.session.add(user_c)
    db.session.commit()

    class FakeAuth:
        def __init__(self, ok, reason=None):
            self.ok = ok
            self.reason = reason
            self.verify_ssl = False

        def login(self, login, password):
            return self.ok

        def get_session(self):
            return None

    class FakeScraper:
        def __init__(self, *a, **kw):
            pass

        def fetch_works(self):
            return [
                {'external_id': '10', 'discipline': 'История C', 'deadline': soon,
                 'status': 'Не отправлена', 'is_overdue': False},
                {'external_id': '11', 'discipline': 'Философия C', 'deadline': past,
                 'status': 'Не отправлена', 'is_overdue': True},
            ]

    orig_auth, orig_scraper, orig_sender = (jobs_mod.build_auth,
                                            jobs_mod.TimelineScraper,
                                            bot_mod.get_sender)
    bot_mod.get_sender = lambda app_: DummySender(OUTBOX)

    # 10.1 обрыв связи != «пароль неверный»: молчим, пока не сломается 3 раза подряд
    jobs_mod._fail_streak.clear()
    jobs_mod._last_alert.clear()
    for _ in range(2):
        reset_outbox()
        jobs_mod.build_auth = lambda app_: FakeAuth(False, REASON_NETWORK)
        assert jobs_mod.check_user(app, user_c.id, force=True) == 'failed'
    assert not [t for _, t in OUTBOX if 'парол' in t.lower()], OUTBOX
    reset_outbox()
    assert jobs_mod.check_user(app, user_c.id, force=True) == 'failed'  # третья подряд
    alerts = [t for _, t in OUTBOX]
    assert alerts and 'не отвечает уже 3' in alerts[0], alerts
    assert not [t for _, t in OUTBOX if 'не принял' in t], OUTBOX
    db.session.expire_all()
    user_c = db.session.get(User, user_c.id)
    assert 'подряд: 3' in (user_c.last_error or ''), user_c.last_error

    # 10.2 явный отказ сайта — вот тут просим сменить данные
    reset_outbox()
    jobs_mod._fail_streak.clear()
    jobs_mod._last_alert.clear()
    jobs_mod.build_auth = lambda app_: FakeAuth(False, REASON_REJECTED)
    assert jobs_mod.check_user(app, user_c.id, force=True) == 'failed'
    assert any('не принял твой логин' in t for _, t in OUTBOX), OUTBOX

    # 10.3 успешная проверка: работы, уведомления, сброс серии неудач
    reset_outbox()
    jobs_mod._fail_streak.clear()
    jobs_mod._last_alert.clear()
    jobs_mod.build_auth = lambda app_: FakeAuth(True)
    jobs_mod.TimelineScraper = FakeScraper
    assert jobs_mod.check_user(app, user_c.id, force=True) == 'ok'
    db.session.expire_all()
    user_c = db.session.get(User, user_c.id)
    assert user_c.last_error is None
    assert WorkItem.query.filter_by(user_id=user_c.id).count() == 2
    assert WorkItem.query.filter_by(user_id=user_a.id).count() == 0  # чужие не тронуты
    joined = '\n'.join(t for _, t in OUTBOX)
    assert 'История C' in joined and 'Философия C' in joined
    assert all(cid == '444' for cid, _ in OUTBOX), OUTBOX

    # 10.4 защита от частых логинов: повторная проверка без force пропускается
    assert jobs_mod.check_user(app, user_c.id) == 'skipped'

    jobs_mod.build_auth, jobs_mod.TimelineScraper = orig_auth, orig_scraper
    bot_mod.get_sender = orig_sender

    # 11. админ-панель: страницы и действия отрабатывают (сеть не трогаем)
    import app.routes.web as web_mod

    web_mod.check_all_users = lambda *a, **k: 0
    web_mod.check_user = lambda *a, **k: 'ok'
    client = app.test_client()

    # 11.1 healthcheck-эндпоинт жив, iCal за ним отдаётся
    assert client.get('/ping').status_code == 200
    ics = client.get(f'/export/ical/{user_b.id}')
    assert ics.status_code == 200 and 'BEGIN:VCALENDAR' in ics.get_data(as_text=True)

    # 11.2 POST без CSRF-токена отклоняется, с токеном — проходит
    assert client.post('/run-check-all').status_code == 400
    assert client.post('/settings', data={'check_interval': '300'}).status_code == 400
    with client.session_transaction() as sess:
        csrf = sess['csrf_token']

    assert client.get(f'/users/{user_b.id}').status_code == 200
    assert client.post('/settings', data={
        'csrf_token': csrf,
        'telegram_bot_token': '0:test', 'check_interval': '300'}).status_code == 302
    assert client.post('/run-check-all', data={'csrf_token': csrf}).status_code == 302
    assert client.post(f'/users/{user_b.id}/update',
                       data={'csrf_token': csrf, 'is_active': '1',
                             'interval_min': '10'}).status_code == 302
    assert client.post(f'/users/{user_b.id}/check', data={'csrf_token': csrf}).status_code == 302
    db.session.expire_all()
    assert db.session.get(User, user_b.id).check_interval == 600

    # пустое поле токена не затирает сохранённый токен (маска в форме вместо него)
    assert client.post('/settings', data={
        'csrf_token': csrf, 'telegram_bot_token': '',
        'check_interval': '300'}).status_code == 302
    from app.models import Setting
    assert (Setting.query.first().telegram_bot_token or '') == '0:test'

    # 11.3 с заданным паролем админка и iCal требуют Basic Auth
    app.config['ADMIN_USER'] = 'admin'
    app.config['ADMIN_PASSWORD'] = 'secret'
    try:
        assert client.get('/').status_code == 401
        assert client.get(f'/export/ical/{user_b.id}').status_code == 401
        auth = {'Authorization': 'Basic ' + base64.b64encode(b'admin:secret').decode()}
        assert client.get('/', headers=auth).status_code == 200
        assert client.get(f'/export/ical/{user_b.id}', headers=auth).status_code == 200
    finally:
        app.config['ADMIN_PASSWORD'] = ''
        app.config['ADMIN_USER'] = ''

    # =========================================================================
    # 12. [v3.1.0] Парсинг детальных полей заданий из SendGrid
    # =========================================================================
    import json as _json_lib
    from app.parser.scraper import TimelineScraper
    from app.services import enrich_work_item

    sample_json_path = os.path.join(_here, 'docs', 'sample_sendgrid_response.json')
    with open(sample_json_path, 'r', encoding='utf-8') as f:
        sample_data = _json_lib.load(f)

    class DummyAuth:
        verify_ssl = False
        def get_session(self):
            class DummySession:
                def get(self, url, **kw):
                    class Resp:
                        def raise_for_status(self): pass
                        def json(self): return sample_data
                        text = _json_lib.dumps(sample_data)
                    return Resp()
            return DummySession()

    scraper = TimelineScraper(DummyAuth(), 'http://fake/send', 'http://fake/grid', semester_id=30)
    works = scraper.fetch_works()
    assert len(works) == 16, f"Ожидалось 16 работ, получено {len(works)}"

    # Проверяем работу 475622 (Психология: есть рецензия, баллы 3 из 3, недоступность)
    w_psy = next(w for w in works if w['external_id'] == '475622')
    assert w_psy['point_name'] == 'Лекция 1. Модуль 2. Возрастная психология.', w_psy['point_name']
    assert w_psy['control_text'] == 'Выполнить работу.', w_psy['control_text']
    assert w_psy['points'] == '3 из 3', w_psy['points']
    assert 'Никита, хорошая работа' in w_psy['teacher_note'], w_psy['teacher_note']
    assert 'Инструмент был доступен' in w_psy['unavailability_reason'], w_psy['unavailability_reason']

    # Проверяем работу 479796 (Нормативно-правовые основы: длинное задание)
    w_law = next(w for w in works if w['external_id'] == '479796')
    assert w_law['point_name'] == 'Анализ сведений с официальных сайтов'
    assert 'Анализ хода выборов' in w_law['control_text']
    assert w_law['points'] == '0 из 10'

    # Сохраняем в БД и проверяем enrich_work_item
    user_v3 = User(chat_id='999333', is_registered=True, is_active=True, state='ready')
    db.session.add(user_v3)
    db.session.commit()

    item_v3 = WorkItem(
        user_id=user_v3.id,
        external_id=w_psy['external_id'],
        discipline=w_psy['discipline'],
        deadline=w_psy['deadline'],
        status=w_psy['status'],
        is_overdue=w_psy['is_overdue'],
        point_name=w_psy['point_name'],
        control_text=w_psy['control_text'],
        points=w_psy['points'],
        teacher_note=w_psy['teacher_note'],
        unavailability_reason=w_psy['unavailability_reason'],
    )
    db.session.add(item_v3)
    db.session.commit()

    enriched = enrich_work_item(item_v3)
    assert enriched['point_name'] == w_psy['point_name']
    assert enriched['points'] == '3 из 3'
    assert enriched['teacher_note'] == w_psy['teacher_note']

    # =========================================================================
    # 13. [v3.1.0] Детальный просмотр задания, <blockquote>, обрезка и команды
    # =========================================================================
    # 13.1 Детальная карточка с цитатами <blockquote>
    card_text, card_kb = bot._build_work_detail(user_v3, w_psy['external_id'])
    assert '<blockquote>' in card_text and '</blockquote>' in card_text
    assert '3 из 3' in card_text
    assert 'Никита, хорошая работа' in card_text
    assert 'Инструмент был доступен' in card_text
    assert any(b.get('url') == 'https://timeline.rsvpu.ru/StudentWork/Send'
               for row in card_kb['inline_keyboard'] for b in row)

    # 13.2 Проверка безопасной обрезки при очень длинном тексте (> 3500 символов)
    item_v3.control_text = 'A' * 4500
    db.session.commit()
    long_card_text, _ = bot._build_work_detail(user_v3, w_psy['external_id'])
    assert len(long_card_text) < 4096, f"Текст превышает лимит Telegram: {len(long_card_text)}"
    assert '[текст обрезан по лимиту]' in long_card_text

    # 13.3 Прямой вызов /task <id>
    reset_outbox()
    bot._handle_message(msg('999333', f"/task {w_psy['external_id']}"))
    assert any('Психология' in t for t in texts()), texts()

    # 13.4 Поиск /find по названию, теме и тексту задания
    reset_outbox()
    bot._handle_message(msg('999333', "/find Возрастная"))
    assert any('Результаты поиска' in t for t in texts()), texts()

    # =========================================================================
    # 14. [v3.1.0] Студенческий гороскоп: детерминизм и 12 знаков
    # =========================================================================
    from app.bot.horoscope import (
        ZODIAC_SIGNS,
        format_horoscope_html,
        generate_horoscope,
        get_daily_horoscope_short,
        normalize_zodiac_sign,
    )

    # 14.1 Все 12 знаков зодиака корректно нормализуются
    assert len(ZODIAC_SIGNS) == 12
    for sign_key in ZODIAC_SIGNS:
        assert normalize_zodiac_sign(sign_key) == sign_key
        assert normalize_zodiac_sign(ZODIAC_SIGNS[sign_key]['name']) == sign_key

    # 14.2 Детерминированность: два вызова в один день дают 100% идентичный результат
    h1 = generate_horoscope('aries')
    h2 = generate_horoscope('aries')
    assert h1['prediction'] == h2['prediction']
    assert h1['lucky_item'] == h2['lucky_item']
    assert h1['advice'] == h2['advice']
    assert h1['avtomat_chance'] == h2['avtomat_chance']

    # 14.3 Форматирование с <blockquote> и короткий вариант для дайджеста
    h_html = format_horoscope_html(h1)
    assert '<blockquote>' in h_html and '</blockquote>' in h_html
    assert 'Шанс автомата' in h_html

    h_short = get_daily_horoscope_short('leo')
    assert '<blockquote>' in h_short and 'Лев' in h_short

    # 14.4 Фембой-локализация гороскопа (:3, лапки, мур-мур)
    user_v3.theme_femboy = True
    h_femboy = generate_horoscope('scorpio', user=user_v3)
    assert h_femboy['is_femboy'] is True
    h_femboy_html = format_horoscope_html(h_femboy)
    assert ':3' in h_femboy_html or '🐾' in h_femboy_html or 'мяу' in h_femboy_html.lower()

    # 14.5 Интерактивный выбор знака через кнопки
    reset_outbox()
    bot._handle_message(msg('999333', "/horoscope"))
    assert any('Выбери' in t or 'гороскоп' in t.lower() for t in texts()), texts()

    # 14.6 Утренний дайджест с включенным гороскопом
    user_v3.digest_notify = True
    user_v3.horoscope_notify = True
    user_v3.zodiac_sign = 'aquarius'
    user_v3.theme_femboy = False
    db.session.commit()

    reset_outbox()
    from app.scheduler.jobs import send_daily_digest
    orig_sender_func = bot_mod.get_sender
    bot_mod.get_sender = lambda _app: DummySender(OUTBOX)
    OUTBOX.clear()
    send_daily_digest(app)
    bot_mod.get_sender = orig_sender_func

    digest_texts = [txt for cid, txt in OUTBOX if cid == '999333']
    assert len(digest_texts) > 0, "Дайджест не был отправлен"
    assert any('Гороскоп дня' in t and 'Водолей' in t for t in digest_texts), digest_texts

    # =========================================================================
    # 16. [v3.1.0] Определение названий и расширений файлов заданий (extract_file_attachments)
    # =========================================================================
    from app.services import extract_file_attachments, extract_urls

    sample_txt = (
        "Выполнить практическую работу №1. Методичка в файле Metodichka_2026.pdf. "
        "Исходные данные: dataset.zip и lab1_variant_05.DOCX. "
        "Ссылка на диск: https://disk.yandex.ru/d/abc123xyz. "
        "Сдать до конца недели."
    )
    files = extract_file_attachments(sample_txt)
    assert files == ['Metodichka_2026.pdf', 'dataset.zip', 'lab1_variant_05.DOCX'], files
    urls = extract_urls(sample_txt)
    assert urls == ['https://disk.yandex.ru/d/abc123xyz'], urls

    # Проверка работы без файлов
    assert extract_file_attachments("Обычный текст задания без файлов. Лекция 1. Модуль 2.") == []

    # Проверка отображения в карточке работы с файлом
    item_file = WorkItem(
        user_id=user_v3.id,
        external_id='999003',
        discipline='Информатика',
        deadline='30.12.2026 23:59',
        status='Не отправлена',
        control_text='Лабораторная работа. Задание в task_01.docx',
    )
    db.session.add(item_file)
    db.session.commit()

    detail_file_text, _ = bot._build_work_detail(user_v3, '999003')
    assert 'Файлы к заданию:' in detail_file_text
    assert '<code>task_01.docx</code>' in detail_file_text

    # Проверка карточки без файлов
    item_nofile = WorkItem(
        user_id=user_v3.id,
        external_id='999004',
        discipline='Философия',
        deadline='30.12.2026 23:59',
        status='Не отправлена',
        control_text='Подготовить устный ответ на вопросы к семинару.',
    )
    db.session.add(item_nofile)
    db.session.commit()

    detail_nofile_text, kb_nofile = bot._build_work_detail(user_v3, '999004')
    assert 'не прикреплено (задание сформулировано текстом)' in detail_nofile_text
    assert any(b.get('url') == 'https://timeline.rsvpu.ru/Timeline' for row in kb_nofile['inline_keyboard'] for b in row)
    assert any(b.get('url') == 'https://timeline.rsvpu.ru/StudentWork/Send' for row in kb_nofile['inline_keyboard'] for b in row)

    # Проверка подсказки при типовой заглушке «Выполнить работу.»
    item_stub = WorkItem(
        user_id=user_v3.id,
        external_id='999005',
        discipline='Психология',
        deadline='30.12.2026 23:59',
        status='Не отправлена',
        control_text='Выполнить работу.',
    )
    db.session.add(item_stub)
    db.session.commit()

    detail_stub_text, _ = bot._build_work_detail(user_v3, '999005')
    assert 'В поле задания указано «Выполнить работу»' in detail_stub_text

    # =========================================================================
    # 17. Проверка кнопки и команды «Помощь», валидность HTML в HELP_TEXT
    # =========================================================================
    import re
    from app.bot.telegram_bot import BTN_HELP, HELP_TEXT

    valid_tg_tags = {'b', 'strong', 'i', 'em', 'u', 'ins', 's', 'strike', 'del', 'span', 'tg-spoiler', 'a', 'tg-emoji', 'code', 'pre', 'blockquote', '/b', '/strong', '/i', '/em', '/u', '/ins', '/s', '/strike', '/del', '/span', '/tg-spoiler', '/a', '/tg-emoji', '/code', '/pre', '/blockquote'}
    all_tags = re.findall(r'<([a-zA-Z0-9_\-А-Яа-яЁё/]+)>', HELP_TEXT)
    invalid_tags = [t for t in all_tags if t.lower() not in valid_tg_tags]
    assert not invalid_tags, f"Недопустимые HTML-теги в HELP_TEXT: {invalid_tags}"

    reset_outbox()
    bot._handle_message(msg('999333', BTN_HELP))
    assert any('Что я умею' in t for t in texts()), texts()

    # =========================================================================
    # 18. [v3.1.1] Редизайн расписания с карточками <blockquote>
    # =========================================================================
    from datetime import date
    from app.parser.schedule import (
        _clean_classroom,
        _format_lesson_block,
        _format_week,
        _shorten_teacher,
        _split_subject_type,
        format_day_compact,
    )

    # 18.1 Разделение названия предмета и типа пары
    assert _split_subject_type('История зарубежной музыки (конс. перед экз.)') == (
        'История зарубежной музыки', 'конс. перед экз.')
    assert _split_subject_type('Музыкально-инструментальное исполнительство (Зачёт с оценкой)') == (
        'Музыкально-инструментальное исполнительство', 'Зачёт с оценкой')
    assert _split_subject_type('История зарубежной музыки (Экз.)') == (
        'История зарубежной музыки', 'Экз.')
    assert _split_subject_type('Физическая культура (элективная) (Практ.зан-я)') == (
        'Физическая культура (элективная)', 'Практ.зан-я')
    assert _split_subject_type('Информатика') == ('Информатика', '')

    # 18.2 Сокращение ФИО преподавателей
    assert _shorten_teacher('Кожевникова А.А.') == 'Кожевникова А.А.'
    assert _shorten_teacher('Кожевникова Алла Олеговна') == 'Кожевникова А.О.'
    assert _shorten_teacher('Иванов Иван Иванович') == 'Иванов И.И.'
    assert _shorten_teacher('Нежинская Т.А.') == 'Нежинская Т.А.'
    assert _shorten_teacher('Петров') == 'Петров'
    assert _shorten_teacher('') == ''

    # 18.3 Очистка названий аудиторий
    assert _clean_classroom('7-203') == '7-203'
    assert _clean_classroom('Ауд. 7-203') == '7-203'
    assert _clean_classroom('Учебная аудитория 469 ГУК') == '469 ГУК'
    assert _clean_classroom('УК М11: Спортивный зал') == 'Спортивный зал'
    assert _clean_classroom('УК М11: Ауд. 201') == '201'
    assert _clean_classroom('Виртуальная аудитория дистант') == 'Дистант'

    # 18.4 Форматирование карточки дня в точном соответствии с макетом
    test_sched_day = {
        'date': date(2026, 6, 18),
        'day_title': '18 июня',
        'weekday_short': 'Чт',
        'lessons': [
            {
                'time': '09:45 - 11:20',
                'subject': 'История зарубежной музыки (конс. перед экз.)',
                'classroom': '7-203',
                'teacher': 'Кожевникова А.А.',
                'subgroup': '',
            },
            {
                'time': '12:00 - 13:35',
                'subject': 'Музыкально-инструментальное исполнительство (Зачёт с оценкой)',
                'classroom': '7-203',
                'teacher': 'Нежинская Т.А.',
                'subgroup': '',
            }
        ]
    }

    rendered_compact = format_day_compact(test_sched_day)
    assert '📅 <b>Четверг, 18.06.2026</b>' in rendered_compact
    assert '<blockquote>' in rendered_compact and '</blockquote>' in rendered_compact
    assert '⏰ 09:45-11:20 | конс. перед экз.' in rendered_compact
    assert '📖 История зарубежной музыки' in rendered_compact
    assert '👩‍🏫 Кожевникова А.А. 🏫 7-203' in rendered_compact
    assert '⏰ 12:00-13:35 | Зачёт с оценкой' in rendered_compact
    assert '📖 Музыкально-инструментальное исполнительство' in rendered_compact
    assert '👩‍🏫 Нежинская Т.А. 🏫 7-203' in rendered_compact

    # 18.5 Проверка дня без пар
    empty_day = {'date': date(2026, 6, 21), 'day_title': '21 июня', 'weekday_short': 'Вс', 'lessons': []}
    rendered_empty = format_day_compact(empty_day)
    assert '<blockquote><i>Пар нет, можно отдыхать! 🎉</i></blockquote>' in rendered_empty

    # 18.6 Недельный вывод с защитой от разрыва тегов Telegram
    week_text = _format_week([test_sched_day, empty_day], 'МКТ-2531', date(2026, 6, 15), date(2026, 6, 21))
    assert '<b>📅 Неделя 15.06 – 21.06 — МКТ-2531</b>' in week_text
    assert week_text.count('<blockquote>') == week_text.count('</blockquote>') == 2

    print(f"OK: все проверки пройдены (шуток: {sum(len(v) for v in JOKES.values())} "
          f"в {len(JOKES)} разделах)")
