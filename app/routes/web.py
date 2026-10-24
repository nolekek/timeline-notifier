import hmac
import logging
import secrets
import threading
from datetime import datetime

from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, current_app, Response, session)

from app.models import db, Setting, User, WorkItem
from app.scheduler.jobs import check_all_users, check_user
from app.bot.telegram_bot import get_sender
from app.services import enrich_work_item as _enrich

logger = logging.getLogger(__name__)

web_bp = Blueprint('web', __name__)


# ------------------------------------------------------------------ админ-доступ

@web_bp.before_request
def _require_admin():
    if request.endpoint == 'web.ping':
        return None  # healthcheck публичный
    user = current_app.config.get('ADMIN_USER') or ''
    password = current_app.config.get('ADMIN_PASSWORD') or ''
    if password:
        auth = request.authorization
        if not (auth and hmac.compare_digest(auth.username or '', user) and
                hmac.compare_digest(auth.password or '', password)):
            return Response('Требуется авторизация', 401,
                            {'WWW-Authenticate': 'Basic realm="Timeline Notifier"'})

    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_hex(16)
    # CSRF: Basic Auth браузер прикрепляет автоматически на любой origin,
    # поэтому все POST-формы несут токен из подписанной сессии
    if request.method == 'POST':
        sent = request.form.get('csrf_token') or request.headers.get('X-CSRF-Token')
        if not sent or not hmac.compare_digest(str(sent), session['csrf_token']):
            return Response('CSRF-токен неверен или отсутствует', 400)
    return None


@web_bp.after_request
def _security_headers(response):
    """Базовые защитные заголовки для веб-панели."""
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline';"
    )
    return response


# ------------------------------------------------------------------ утилиты

def _global_interval(setting) -> int:
    return (setting.check_interval if setting and setting.check_interval
            else current_app.config['DEFAULT_CHECK_INTERVAL'])


# ------------------------------------------------------------------- страницы

@web_bp.route('/')
def index():
    setting = Setting.query.first() or Setting()
    users = User.query.order_by(User.created_at.desc()).all()
    default_interval = _global_interval(setting)

    # все работы одним запросом (вместо запроса на каждого пользователя)
    works_by_user = {}
    for w in WorkItem.query.all():
        works_by_user.setdefault(w.user_id, []).append(w)

    rows = []
    for u in users:
        works = works_by_user.get(u.id, [])
        overdue = sum(1 for w in works if _enrich(w)['is_overdue'])
        rows.append({
            'user': u,
            'works': len(works),
            'overdue': overdue,
            'interval': (u.check_interval or default_interval) // 60,
        })

    stats = {
        'users': len(users),
        'registered': sum(1 for u in users if u.is_registered),
        'active': sum(1 for u in users if u.is_registered and u.is_active),
        'works': sum(r['works'] for r in rows),
    }
    return render_template('index.html', setting=setting, rows=rows, stats=stats)


@web_bp.route('/settings', methods=['POST'])
def save_settings():
    setting = Setting.query.first()
    if not setting:
        setting = Setting()
        db.session.add(setting)

    # пустое поле = «не трогать токен»: сам токен в страницу не рендерится
    new_token = request.form.get('telegram_bot_token', '').strip()
    if new_token:
        setting.telegram_bot_token = new_token
    try:
        setting.check_interval = max(60, int(request.form.get('check_interval', 300)))
    except ValueError:
        setting.check_interval = 300
    db.session.commit()

    if request.form.get('restart_bot'):
        from app.bot.telegram_bot import start_bot
        try:
            start_bot(current_app._get_current_object())
            flash('Токен сохранён, бот перезапущен', 'success')
        except Exception as e:
            flash(f'Токен сохранён, но бот не запустился: {e}', 'error')
    else:
        flash('Настройки сохранены', 'success')
    return redirect(url_for('web.index'))


@web_bp.route('/run-check-all', methods=['POST'])
def run_check_all():
    import threading
    app = current_app._get_current_object()
    threading.Thread(target=check_all_users, args=(app,), kwargs={'force': True},
                     daemon=True).start()
    flash('Проверка всех пользователей запущена в фоне', 'info')
    return redirect(url_for('web.index'))


@web_bp.route('/users/<int:user_id>')
def user_detail(user_id: int):
    user = db.session.get(User, user_id)
    if user is None:
        flash('Пользователь не найден', 'error')
        return redirect(url_for('web.index'))
    setting = Setting.query.first()
    raw = (WorkItem.query.filter_by(user_id=user.id)
           .order_by(WorkItem.last_updated_at.desc()).limit(200).all())
    works = [_enrich(w) for w in raw]
    return render_template('user.html', user=user, works=works,
                           default_interval=_global_interval(setting) // 60)


@web_bp.route('/users/<int:user_id>/update', methods=['POST'])
def user_update(user_id: int):
    user = db.session.get(User, user_id)
    if user is None:
        flash('Пользователь не найден', 'error')
        return redirect(url_for('web.index'))

    user.is_active = bool(request.form.get('is_active'))
    user.notify_enabled = bool(request.form.get('notify_enabled'))
    if request.form.get('group_name'):
        user.group_name = request.form.get('group_name').strip()[:50].upper()
    try:
        minutes = int(request.form.get('interval_min') or 0)
    except ValueError:
        minutes = 0
    user.check_interval = minutes * 60 if minutes >= 5 else 0
    db.session.commit()
    flash('Пользователь обновлён', 'success')
    return redirect(url_for('web.user_detail', user_id=user.id))


@web_bp.route('/users/<int:user_id>/check', methods=['POST'])
def user_check(user_id: int):
    user = db.session.get(User, user_id)
    if user is None:
        flash('Пользователь не найден', 'error')
        return redirect(url_for('web.index'))
    # логин в Timeline с ретраями может занять минуту — не вешаем HTTP-запрос
    threading.Thread(
        target=check_user,
        args=(current_app._get_current_object(), user.id),
        kwargs={'notify': False, 'force': True},
        daemon=True,
    ).start()
    flash('Проверка запущена — результат появится в карточке через пару минут', 'info')
    return redirect(url_for('web.user_detail', user_id=user.id))


@web_bp.route('/users/<int:user_id>/test', methods=['POST'])
def user_test(user_id: int):
    user = db.session.get(User, user_id)
    sender = get_sender(current_app._get_current_object())
    if user is None or sender is None:
        flash('Не настроен токен бота', 'error')
        return redirect(url_for('web.index'))
    ok = sender.send_message(user.chat_id, '✅ Тестовое сообщение от Timeline Notifier')
    flash('Отправлено' if ok else 'Не удалось отправить', 'success' if ok else 'error')
    return redirect(url_for('web.user_detail', user_id=user.id))


@web_bp.route('/users/<int:user_id>/delete', methods=['POST'])
def user_delete(user_id: int):
    user = db.session.get(User, user_id)
    if user is None:
        flash('Пользователь не найден', 'error')
        return redirect(url_for('web.index'))
    chat_id = user.chat_id
    db.session.delete(user)
    db.session.commit()
    flash(f'Пользователь {chat_id} удалён', 'info')
    return redirect(url_for('web.index'))


@web_bp.route('/bot/restart', methods=['POST'])
def bot_restart():
    from app.bot.telegram_bot import start_bot
    bot = start_bot(current_app._get_current_object())
    flash('Бот перезапущен' if bot else 'Токен не задан', 'success' if bot else 'error')
    return redirect(url_for('web.index'))


@web_bp.route('/ping')
def ping():
    """Healthcheck для Docker / мониторинга."""
    from flask import jsonify
    try:
        db.session.execute(db.text('SELECT 1'))
        db_ok = True
    except Exception:
        db_ok = False
    status = 200 if db_ok else 503
    return jsonify({'status': 'ok' if db_ok else 'db_error', 'db': db_ok}), status


@web_bp.route('/export/ical/<int:user_id>')
def export_ical(user_id: int):
    """Экспорт дедлайнов пользователя в iCalendar (.ics) — под Basic Auth."""
    user = db.session.get(User, user_id)
    if not user:
        return Response('Пользователь не найден', 404)

    from datetime import timedelta

    def _ics_escape(text: str) -> str:
        """Экранирование по RFC 5545: бэкслеш, точка с запятой, запятая, переносы."""
        return (str(text or '').replace('\\', '\\\\').replace(';', '\\;')
                .replace(',', '\\,').replace('\r\n', '\\n').replace('\n', '\\n'))

    works = WorkItem.query.filter_by(user_id=user.id).all()

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Timeline Notifier//RU",
        "CALSCALE:GREGORIAN",
        f"X-WR-CALNAME:{_ics_escape('Дедлайны - ' + user.title)}",
    ]

    for w in works:
        if not w.deadline:
            continue
        try:
            dt = datetime.strptime(w.deadline, '%d.%m.%Y %H:%M')
        except ValueError:
            continue

        # iCal формат дат YYYYMMDDTHHMMSS
        dt_start = (dt - timedelta(hours=1)).strftime('%Y%m%dT%H%M00')
        dt_end = dt.strftime('%Y%m%dT%H%M00')
        uid = f"work-{w.external_id}-{w.id}@timeline"
        status_txt = w.status or 'Не отправлена'

        lines.extend([
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"SUMMARY:{_ics_escape('Дедлайн: ' + w.discipline)}",
            f"DESCRIPTION:{_ics_escape(f'Дисциплина: {w.discipline}\\nСтатус: {status_txt}\\nСрок сдачи: {w.deadline}')}",
            f"DTSTART:{dt_start}",
            f"DTEND:{dt_end}",
            "STATUS:CONFIRMED",
            "END:VEVENT",
        ])

    lines.append("END:VCALENDAR")
    ics_content = "\r\n".join(lines)
    return Response(
        ics_content,
        mimetype="text/calendar",
        headers={"Content-Disposition": f"attachment; filename=deadlines_{user.id}.ics"}
    )
