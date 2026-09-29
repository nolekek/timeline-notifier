"""Быстрая проверка логики без сети.

Запуск: python tests/smoke_test.py     (ожидаемый вывод: OK: все проверки пройдены)

Что проверяем: онбординг через бота, изоляцию данных между пользователями, напоминания,
core-цикл планировщика (включая честный разбор причин неудачного входа), нижнее меню,
страницы админки. Сеть не используется: Timeline и Telegram подменены заглушками,
БД — временная (instance/smoke-test.db, удаляется на выходе).
"""
import atexit
import os
import random
import sys
import threading
from datetime import datetime, timedelta
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

from app import create_app  # noqa: E402
import app.bot.telegram_bot as bot_mod  # noqa: E402
import app.parser.auth as auth_mod  # noqa: E402
import app.scheduler.jobs as jobs_mod  # noqa: E402
from app.bot.jokes import JOKES  # noqa: E402
from app.bot.telegram_bot import BTN_CHECK, BTN_STATUS, SORT_DEADLINE, TelegramBot  # noqa: E402
from app.models import db, User, WorkItem  # noqa: E402
from app.parser.auth import REASON_NETWORK, REASON_REJECTED  # noqa: E402
from app.scheduler.jobs import _check_deadline_reminders  # noqa: E402
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
    base = datetime.utcnow()
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
        'noop', 'st|new|2', 'st|dl|1'], kb1

    page2, kb2 = bot._build_status_page(user_p, 2)
    assert 'Работа 4' in page2 and 'Работа 0' not in page2, page2
    assert any(j in page2 for j in JOKES['status_last']), page2
    assert [b['callback_data'] for row in kb2['inline_keyboard'] for b in row] == [
        'st|new|1', 'noop', 'st|dl|1'], kb2

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
    assert client.get('/').status_code == 200
    assert client.get(f'/users/{user_b.id}').status_code == 200
    assert client.post('/settings', data={
        'telegram_bot_token': '0:test', 'check_interval': '300'}).status_code == 302
    assert client.post('/run-check-all').status_code == 302
    assert client.post(f'/users/{user_b.id}/update',
                       data={'is_active': '1', 'interval_min': '10'}).status_code == 302
    assert client.post(f'/users/{user_b.id}/check').status_code == 302
    db.session.expire_all()
    assert db.session.get(User, user_b.id).check_interval == 600

    print(f"OK: все проверки пройдены (шуток: {sum(len(v) for v in JOKES.values())} "
          f"в {len(JOKES)} разделах)")
