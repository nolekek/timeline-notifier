import logging
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler

from app.models import db, Setting, User, WorkItem
from app.parser.auth import (REASON_NETWORK, REASON_REJECTED, REASON_TEXT,
                             build_auth, debug_dir)
from app.parser.scraper import SKIP_STATUSES, TimelineScraper
from app.security import decrypt

logger = logging.getLogger(__name__)

_scheduler = None

NOT_SENT_STATUSES = ('', 'Не отправлена')

# Результаты check_user()
CHECK_OK = 'ok'
CHECK_FAILED = 'failed'
CHECK_SKIPPED = 'skipped'
CHECK_UNREGISTERED = 'unregistered'

# Сколько раз подряд должно сломаться, чтобы беспокоить человека/админа
ALERT_AT_STREAK = (3, 10, 30)

# Состояние серии неудач — в памяти процесса (для подавления ложных тревог)
_fail_streak = defaultdict(int)
_last_alert = {}
_locks = {}
_locks_guard = threading.Lock()


def _user_lock(user_id: int) -> threading.Lock:
    """Один вход в Timeline на пользователя одновременно."""
    with _locks_guard:
        lock = _locks.get(user_id)
        if lock is None:
            lock = threading.Lock()
            _locks[user_id] = lock
        return lock


def mask_chat(chat_id) -> str:
    """Telegram-id в логи не пишем целиком: ***7769.

    Полный chat_id всегда виден в админке, а в логах он только засоряет вывод.
    """
    s = str(chat_id or '')
    return '***' + s[-4:] if len(s) > 4 else '***'


# ------------------------------------------------------------------ напоминания

def _check_deadline_reminders(items):
    """items — работы ОДНОГО пользователя."""
    now = datetime.now()
    groups = {48: [], 24: [], 12: []}
    overdue = []

    for item in items:
        status = item.status or ''
        if status not in NOT_SENT_STATUSES:
            continue
        if not item.deadline:
            continue

        try:
            deadline_dt = datetime.strptime(item.deadline, '%d.%m.%Y %H:%M')
        except ValueError:
            continue

        payload = {
            'discipline': item.discipline,
            'deadline': item.deadline,
            'status': status or 'Не отправлена',
        }
        hours_left = (deadline_dt - now).total_seconds() / 3600

        if hours_left < 0:
            if not item.overdue_sent:
                overdue.append(payload)
                item.overdue_sent = True
                item.reminder_48_sent = True
                item.reminder_24_sent = True
                item.reminder_12_sent = True
            continue

        if hours_left <= 12:
            if not item.reminder_12_sent:
                groups[12].append(payload)
                item.reminder_48_sent = True
                item.reminder_24_sent = True
                item.reminder_12_sent = True
        elif hours_left <= 24:
            if not item.reminder_24_sent:
                groups[24].append(payload)
                item.reminder_48_sent = True
                item.reminder_24_sent = True
        elif hours_left <= 48:
            if not item.reminder_48_sent:
                groups[48].append(payload)
                item.reminder_48_sent = True

    return groups, overdue


# ------------------------------------------------------------- проверка юзера

def check_user(app, user_id: int, notify: bool = True, force: bool = False) -> str:
    """Проверка одного пользователя.

    Возвращает CHECK_OK / CHECK_FAILED / CHECK_SKIPPED / CHECK_UNREGISTERED.
    force=True — игнорировать MIN_CHECK_GAP (для регистрации и ручных проверок).
    """
    lock = _user_lock(user_id)
    if not lock.acquire(blocking=False):
        logger.info(f"Пользователь {user_id} уже проверяется — пропускаю дубль")
        return CHECK_SKIPPED
    try:
        return _check_user_locked(app, user_id, notify, force)
    except Exception:
        logger.exception(f"Проверка пользователя {user_id} упала")
        return CHECK_FAILED
    finally:
        lock.release()


def _check_user_locked(app, user_id: int, notify: bool, force: bool) -> str:
    from app.bot.telegram_bot import (get_sender, notify_admin, notify_deadline_reminders,
                                      notify_error, notify_new_works, notify_overdue,
                                      notify_status_changes)

    with app.app_context():
        user = db.session.get(User, user_id)
        if user is None or not user.is_registered or not user.password_enc:
            return CHECK_UNREGISTERED

        # не долбим сайт чаще, чем раз в MIN_CHECK_GAP секунд
        min_gap = int(app.config.get('MIN_CHECK_GAP', 60))
        if not force and user.last_check_at:
            age = (datetime.utcnow() - user.last_check_at).total_seconds()
            if age < min_gap:
                logger.info(f"Проверка chat_id={mask_chat(user.chat_id)} пропущена: "
                            f"проверяли {int(age)} с назад")
                return CHECK_SKIPPED

        password = decrypt(user.password_enc)
        if not password:
            # CRED_KEY сменился/потерялся — пароль не прочитать
            user.last_error = 'не удалось расшифровать пароль (сменился CRED_KEY?)'
            user.last_check_at = datetime.utcnow()
            db.session.commit()
            logger.error(f"Не удалось расшифровать пароль: chat_id={mask_chat(user.chat_id)}")
            if notify and user.notify_enabled and _last_alert.get(user_id) != 'crypt':
                sender = get_sender(app)
                if sender:
                    notify_error(sender, user.chat_id,
                                 "⚠️ Не могу прочитать твой пароль в базе бота.\n"
                                 "Пришли данные заново: /login")
                    _last_alert[user_id] = 'crypt'
            return CHECK_FAILED

        auth = build_auth(app)
        if not auth.login(user.timeline_login, password):
            _handle_login_failure(app, user, auth.reason, notify)
            user.last_check_at = datetime.utcnow()
            db.session.commit()
            return CHECK_FAILED

        # --- вход удался: сбрасываем серию неудач -------------------------------
        _fail_streak.pop(user_id, None)
        _last_alert.pop(user_id, None)

        scraper = TimelineScraper(
            auth,
            send_url=app.config['TIMELINE_WORKS_URL'],
            grid_url=app.config['TIMELINE_GRID_URL'],
            debug_dir=debug_dir(app),
        )
        works = scraper.fetch_works()

        new_works = []
        status_changes = []

        if works:
            seen_ids = set()
            for w in works:
                seen_ids.add(w['external_id'])
                item = WorkItem.query.filter_by(user_id=user.id,
                                                external_id=w['external_id']).first()

                if item is None:
                    item = WorkItem(
                        user_id=user.id,
                        external_id=w['external_id'],
                        discipline=w['discipline'],
                        deadline=w['deadline'],
                        status=w['status'],
                    )
                    if w['status'] in SKIP_STATUSES:
                        item.notified_new = True
                    db.session.add(item)
                    if w['status'] not in SKIP_STATUSES:
                        new_works.append(w)
                else:
                    old_status = item.status or ''
                    new_status = w['status'] or ''

                    if w['deadline'] and item.deadline != w['deadline']:
                        item.deadline = w['deadline']
                        item.reminder_48_sent = False
                        item.reminder_24_sent = False
                        item.reminder_12_sent = False
                        item.overdue_sent = False

                    item.discipline = w['discipline']

                    if old_status != new_status:
                        status_changes.append({
                            'discipline': w['discipline'],
                            'deadline': w['deadline'],
                            'old_status': old_status,
                            'new_status': new_status,
                            'is_overdue': w.get('is_overdue', False),
                        })
                        item.status = new_status

            # работы, исчезнувшие из ведомости, больше не актуальны
            for stale in WorkItem.query.filter_by(user_id=user.id).all():
                if stale.external_id not in seen_ids:
                    db.session.delete(stale)

        db.session.commit()

        sender = get_sender(app) if notify else None
        if sender and user.notify_enabled:
            if new_works:
                notify_new_works(sender, user.chat_id, new_works)
            if status_changes:
                notify_status_changes(sender, user.chat_id, status_changes)

        items = WorkItem.query.filter_by(user_id=user.id).all()
        groups, overdue = _check_deadline_reminders(items)
        if sender and user.notify_enabled:
            if any(groups.values()):
                notify_deadline_reminders(sender, user.chat_id, groups)
            if overdue:
                notify_overdue(sender, user.chat_id, overdue)

        if user.last_error:
            user.last_error = None
        user.last_check_at = datetime.utcnow()
        db.session.commit()

        logger.info(f"Проверка chat_id={mask_chat(user.chat_id)}: работ {len(works)}, "
                    f"новых {len(new_works)}, изменений {len(status_changes)}")
        return CHECK_OK


def _handle_login_failure(app, user, reason: str, notify: bool):
    """Разбор неудачного входа: данные пользователя или проблемы сайта."""
    from app.bot.telegram_bot import get_sender, notify_admin, notify_error

    streak = _fail_streak[user.id] + 1
    _fail_streak[user.id] = streak
    text = REASON_TEXT.get(reason, reason or 'неизвестная ошибка')
    user.last_error = f"{text} (подряд: {streak})"
    db.session.commit()
    logger.warning(f"Авторизация не удалась: chat_id={mask_chat(user.chat_id)}, "
                   f"причина: {reason}, подряд: {streak}")

    if not notify:
        return
    sender = get_sender(app)
    if sender is None:
        return

    if reason == REASON_REJECTED:
        # единственный случай, когда виноваты данные пользователя
        if _last_alert.get(user.id) != REASON_REJECTED:
            if user.notify_enabled:
                notify_error(sender, user.chat_id,
                             "⚠️ Timeline не принял твой логин или пароль.\n"
                             "Если ты менял пароль — пришли данные заново: /login")
            _last_alert[user.id] = REASON_REJECTED
        return

    # сетевая проблема/поломка сайта — пароль тут ни при чём
    if streak in ALERT_AT_STREAK:
        if user.notify_enabled:
            if reason == REASON_NETWORK:
                notify_error(sender, user.chat_id,
                             f"⚠️ Timeline не отвечает уже {streak} проверок подряд.\n"
                             "С твоими данными всё в порядке — это сбои на стороне сайта, "
                             "попробую снова сам.")
            else:
                notify_error(sender, user.chat_id,
                             "⚠️ Не могу получить данные из Timeline: "
                             f"{text}.\nДело не в твоём пароле — я сообщил администратору.")
        admin = app.config.get('ADMIN_CHAT_ID')
        if admin and streak == ALERT_AT_STREAK[0]:
            notify_admin(sender, admin,
                         f"🛠 Timeline Notifier: {text}\n"
                         f"chat_id={user.chat_id}, подряд {streak}. "
                         f"Смотри логи и instance/debug.")


# --------------------------------------------------------------- все юзеры

def _due_users(app, force: bool = False) -> list[int]:
    with app.app_context():
        setting = Setting.query.first()
        default_interval = ((setting.check_interval if setting else None)
                            or app.config['DEFAULT_CHECK_INTERVAL'])
        now = datetime.utcnow()
        due = []
        users = User.query.filter_by(is_registered=True, is_active=True).all()
        for u in users:
            if force or u.last_check_at is None:
                due.append(u.id)
                continue
            interval = u.check_interval or default_interval
            if (now - u.last_check_at).total_seconds() >= interval:
                due.append(u.id)
        return due


def check_all_users(app, force: bool = False) -> int:
    """Тик планировщика: проверяет тех, кому пора (force=True — всех)."""
    user_ids = _due_users(app, force=force)
    if not user_ids:
        return 0

    workers = max(1, int(app.config.get('CHECK_WORKERS', 4)))
    logger.info(f"Проверяю пользователей: {len(user_ids)} (потоков {workers})")

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(check_user, app, uid): uid for uid in user_ids}
        for fut in as_completed(futures):
            uid = futures[fut]
            try:
                if fut.result() == CHECK_OK:
                    done += 1
            except Exception as e:
                logger.error(f"Проверка пользователя {uid} упала: {e}", exc_info=True)

    with app.app_context():
        setting = Setting.query.first()
        if setting:
            setting.last_check_at = datetime.utcnow()
            db.session.commit()
    return done


def start_scheduler(app):
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    tick = int(app.config.get('SCHEDULER_TICK', 60))
    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(
        func=check_all_users,
        args=[app],
        trigger='interval',
        seconds=tick,
        id='check_all_users',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()
    logger.info(f"Планировщик запущен: тик {tick} сек")
    return _scheduler
