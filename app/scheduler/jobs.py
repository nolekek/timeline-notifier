import json
import logging
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler

from app.models import db, Setting, User, WorkItem, utc_now
from app.parser.auth import (REASON_NETWORK, REASON_REJECTED, REASON_TEXT,
                             build_auth, debug_dir)
from app.parser.scraper import SKIP_STATUSES, TimelineScraper
from app.security import decrypt, encrypt
from app.services import NOT_SENT_STATUSES

logger = logging.getLogger(__name__)

_scheduler = None

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


def _due_daily(now: datetime, notify_time: str, last_sent: datetime | None,
               default_hour: int = 8, default_minute: int = 0) -> bool:
    """Пора ли слать ежедневное уведомление.

    Отправляем с настроенного времени до конца дня, но один раз в день
    (last_sent хранится в БД — рестарты и сдвиг фазы тика не дают дублей).
    """
    try:
        h, m = map(int, (notify_time or '').split(':')[:2])
    except (ValueError, TypeError):
        h, m = default_hour, default_minute
    if not (0 <= h <= 23 and 0 <= m <= 59):
        h, m = default_hour, default_minute  # «25:99» — это опечатка, а не 23:59
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if now < target:
        return False
    if last_sent and last_sent.date() == now.date():
        return False
    return True


def _user_lock(user_id: int) -> threading.Lock:
    """Один вход в Timeline на пользователя одновременно."""
    with _locks_guard:
        lock = _locks.get(user_id)
        if lock is None:
            lock = threading.Lock()
            _locks[user_id] = lock
        return lock


def mask_chat(chat_id) -> str:
    """Telegram-id в логи не пишем целиком: ***7769."""
    s = str(chat_id or '')
    return '***' + s[-4:] if len(s) > 4 else '***'


# ------------------------------------------------------------------ напоминания

def _check_deadline_reminders(items, send: bool = True):
    """items — работы ОДНОГО пользователя.

    send=False используется при ручных проверках (когда результат показывается на
    экране, а не уходит уведомлением): тогда флаги «напоминание отправлено» НЕ
    взводятся, иначе первое же ручное нажатие молча съело бы напоминание.
    """
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
                if send:
                    item.overdue_sent = True
                    item.reminder_48_sent = True
                    item.reminder_24_sent = True
                    item.reminder_12_sent = True
            continue

        if hours_left <= 12:
            if not item.reminder_12_sent:
                groups[12].append(payload)
                if send:
                    item.reminder_48_sent = True
                    item.reminder_24_sent = True
                    item.reminder_12_sent = True
        elif hours_left <= 24:
            if not item.reminder_24_sent:
                groups[24].append(payload)
                if send:
                    item.reminder_48_sent = True
                    item.reminder_24_sent = True
        elif hours_left <= 48:
            if not item.reminder_48_sent:
                groups[48].append(payload)
                if send:
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
            age = (utc_now() - user.last_check_at).total_seconds()
            if age < min_gap:
                logger.info(f"Проверка chat_id={mask_chat(user.chat_id)} пропущена: "
                            f"проверяли {int(age)} с назад")
                return CHECK_SKIPPED

        password = decrypt(user.password_enc)
        if not password:
            # CRED_KEY сменился/потерялся — пароль не прочитать
            user.last_error = 'не удалось расшифровать пароль (сменился CRED_KEY?)'
            user.last_check_at = utc_now()
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

        # Оптимизация: пробуем сначала сохраненную сессию (куки в БД зашифрованы)
        stored_cookies = decrypt(user.session_cookies) if user.session_cookies else None
        try:
            auth = build_auth(app, stored_cookies)
        except TypeError:
            auth = build_auth(app)

        session_alive = getattr(auth, 'is_session_alive', lambda: False)()

        if session_alive:
            logger.info(f"Сессия chat_id={mask_chat(user.chat_id)} ещё активна, повторный вход пропущен")
        elif not auth.login(user.timeline_login, password):
            _handle_login_failure(app, user, auth.reason, notify)
            user.last_check_at = utc_now()
            user.session_cookies = None
            db.session.commit()
            return CHECK_FAILED
        else:
            if hasattr(auth, 'dump_cookies'):
                user.session_cookies = encrypt(json.dumps(auth.dump_cookies()))
            db.session.commit()

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
            if not getattr(user, 'group_manually_set', False):
                from app.parser.scraper import extract_group_from_works
                detected_group = extract_group_from_works(works)
                if detected_group and detected_group != user.group_name:
                    logger.info(f"Автоматически определена группа пользователя {user.id}: {detected_group} (была: {user.group_name})")
                    user.group_name = detected_group

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
        will_notify = bool(sender and user.notify_enabled)
        if will_notify:
            if new_works:
                notify_new_works(sender, user.chat_id, new_works)
            if status_changes:
                notify_status_changes(sender, user.chat_id, status_changes)

        items = WorkItem.query.filter_by(user_id=user.id).all()
        groups, overdue = _check_deadline_reminders(items, send=will_notify)
        if will_notify:
            if any(groups.values()):
                notify_deadline_reminders(sender, user.chat_id, groups)
            if overdue:
                notify_overdue(sender, user.chat_id, overdue)

        if user.last_error:
            user.last_error = None
        user.last_check_at = utc_now()
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
        now = utc_now()
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
            setting.last_check_at = utc_now()
        # прибираем in-memory счётчики удалённых пользователей
        try:
            existing = {row[0] for row in db.session.query(User.id).all()}
            for uid in [k for k in _fail_streak if k not in existing]:
                _fail_streak.pop(uid, None)
                _last_alert.pop(uid, None)
        except Exception as e:
            logger.debug(f"Чистка счётчиков неудач пропущена: {e}")
        db.session.commit()
    return done


def send_daily_digest(app):
    """Утренний дайджест: горящие дедлайны для подписанных юзеров.

    Уходит один раз в день после настроенного времени (время+дата последней
    отправки хранятся в БД, поэтому дублирований из-за частого тика или
    рестарта не бывает).
    """
    from app.bot.telegram_bot import get_sender
    import html as html_mod

    now = datetime.now()
    with app.app_context():
        users = User.query.filter_by(is_registered=True, is_active=True,
                                     notify_enabled=True,
                                     digest_notify=True).all()
        for user in users:
            if not _due_daily(now, getattr(user, 'digest_notify_time', None) or '08:00',
                              getattr(user, 'digest_last_sent', None),
                              default_hour=8, default_minute=0):
                continue

            items = WorkItem.query.filter_by(user_id=user.id).all()
            urgent = []
            for it in items:
                if (it.status or '') not in NOT_SENT_STATUSES:
                    continue
                if not it.deadline:
                    continue
                try:
                    dt = datetime.strptime(it.deadline, '%d.%m.%Y %H:%M')
                except ValueError:
                    continue
                delta = (dt - now).total_seconds()
                if 0 < delta <= 72 * 3600:
                    urgent.append((dt, it))

            user.digest_last_sent = now  # помечаем обработанным и без горящих работ
            if not urgent:
                db.session.commit()
                continue
            urgent.sort(key=lambda x: x[0])

            lines = ['<b>🌅 Доброе утро! Дедлайны ближайших 3 дней:</b>\n']
            for dt, it in urgent[:10]:
                hours_left = (dt - now).total_seconds() / 3600
                if hours_left < 12:
                    icon = '🚨'
                elif hours_left < 24:
                    icon = '🔴'
                else:
                    icon = '⏳'
                lines.append(f'{icon} <b>{html_mod.escape(it.discipline)}</b>')
                lines.append(f'   Срок: {it.deadline}')
                lines.append('')

            sender = get_sender(app)
            if sender and sender.send_message(user.chat_id, '\n'.join(lines)):
                db.session.commit()


def send_schedule_notify(app):
    """Вечернее уведомление — расписание на завтра (один раз в день)."""
    from app.bot.telegram_bot import get_sender
    from app.parser.schedule import get_schedule_view

    now = datetime.now()
    with app.app_context():
        users = User.query.filter_by(is_registered=True, is_active=True,
                                     notify_enabled=True,
                                     schedule_notify=True).all()
        for user in users:
            if not _due_daily(now, getattr(user, 'schedule_notify_time', None) or '22:00',
                              getattr(user, 'schedule_last_sent', None),
                              default_hour=22, default_minute=0):
                continue

            try:
                group = user.current_group if hasattr(user, 'current_group') else 'МКТ-2531'
                text = get_schedule_view(group, mode='tomorrow')
                sender = get_sender(app)
                # помечаем отправленным только при успехе — сбой сайта не должен
                # «съедать» вечернее расписание
                if sender and sender.send_message(
                        user.chat_id, '🌙 <b>Расписание на завтра:</b>\n\n' + text):
                    user.schedule_last_sent = now
                    db.session.commit()
            except Exception as e:
                logger.error(f'send_schedule_notify: {e}')


def check_schedule_changes(app):
    """Контроль изменений в расписании (раз в час).

    Следит и уведомляет ТОЛЬКО те пользователи, которые сами включили
    уведомления о расписании (schedule_notify) и не выключали уведомления
    вообще (notify_enabled) — остальным сайт не дёргаем и не пишем.
    """
    import hashlib
    import json
    from app.bot.telegram_bot import get_sender
    from app.parser.schedule import get_group_schedule
    import html as html_mod

    with app.app_context():
        users = User.query.filter_by(is_registered=True, is_active=True,
                                     notify_enabled=True,
                                     schedule_notify=True).all()
        if not users:
            return
        # Группируем по уникальным группам чтобы не парсить одну группу 10 раз
        groups_map = {}
        for u in users:
            grp = u.current_group if hasattr(u, 'current_group') else 'МКТ-2531'
            groups_map.setdefault(grp, []).append(u)

        for group_name, group_users in groups_map.items():
            try:
                # Получаем свежие данные с сайта без кэша
                days = get_group_schedule(group_name, force_refresh=True)
                if not days:
                    continue
                # Превращаем структуру дней/пар в стабильный JSON для хэширования
                serialized = []
                for d in days:
                    serialized.append({
                        'day': d.get('day_title', ''),
                        'lessons': d.get('lessons', [])
                    })
                dump = json.dumps(serialized, sort_keys=True, ensure_ascii=False)
                new_hash = hashlib.md5(dump.encode('utf-8')).hexdigest()

                sender = get_sender(app)
                for u in group_users:
                    old_hash = getattr(u, 'schedule_hash', None)
                    if not old_hash:
                        u.schedule_hash = new_hash
                        continue
                    if old_hash != new_hash:
                        u.schedule_hash = new_hash
                        if sender:
                            sender.send_message(
                                u.chat_id,
                                f"📢 <b>Внимание: расписание группы {html_mod.escape(group_name)} изменилось!</b>\n"
                                "Проверь обновлённые пары через кнопку «📅 Расписание» или /schedule"
                            )
                db.session.commit()
            except Exception as e:
                logger.error(f"check_schedule_changes error for group {group_name}: {e}")


def start_scheduler(app):
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    # Отключаем спам в логи о каждом тике APScheduler (бережет flash/SD-карту и CPU)
    logging.getLogger('apscheduler.executors.default').setLevel(logging.WARNING)

    low_power = str(app.config.get('LOW_POWER_MODE', 'true')).lower() in ('1', 'true', 'yes')
    _scheduler = BackgroundScheduler(daemon=True)

    if low_power:
        # Режим Low Power Max: проверки 3 раза в день (8:00, 14:00, 20:00), CPU спит 99% времени
        check_hours = app.config.get('CHECK_HOURS', '8,14,20')
        _scheduler.add_job(
            func=check_all_users,
            args=[app, True],  # force=True, чтобы точно проверить всех по расписанию
            trigger='cron',
            hour=check_hours,
            minute=0,
            id='check_all_users',
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        # Утренний дайджест дедлайнов (проверка раз в 10 минут)
        _scheduler.add_job(
            func=send_daily_digest,
            args=[app],
            trigger='interval',
            minutes=10,
            id='daily_digest',
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        # Вечернее расписание на завтра (проверка раз в 10 минут)
        _scheduler.add_job(
            func=send_schedule_notify,
            args=[app],
            trigger='interval',
            minutes=10,
            id='schedule_notify',
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        # Мониторинг изменений расписания (2 раза в день: утром и вечером)
        _scheduler.add_job(
            func=check_schedule_changes,
            args=[app],
            trigger='cron',
            hour='7,19',
            minute=0,
            id='schedule_changes',
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        logger.info(f"Планировщик запущен в режиме Low-Power: проверка дедлайнов в {check_hours}:00")
    else:
        tick = int(app.config.get('SCHEDULER_TICK', 60))
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
        _scheduler.add_job(
            func=send_daily_digest,
            args=[app],
            trigger='interval',
            seconds=60,
            id='daily_digest',
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        _scheduler.add_job(
            func=send_schedule_notify,
            args=[app],
            trigger='interval',
            seconds=60,
            id='schedule_notify',
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        _scheduler.add_job(
            func=check_schedule_changes,
            args=[app],
            trigger='interval',
            seconds=3600,
            id='schedule_changes',
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        logger.info(f"Планировщик запущен: тик {tick} сек")

    _scheduler.start()
    return _scheduler
