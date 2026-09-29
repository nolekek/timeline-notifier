import hmac
import logging
from datetime import datetime

from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, current_app, Response)

from app.models import db, Setting, User, WorkItem
from app.scheduler.jobs import check_all_users, check_user
from app.bot.telegram_bot import get_sender

logger = logging.getLogger(__name__)

web_bp = Blueprint('web', __name__)

NOT_SENT_STATUSES = ('', 'Не отправлена')


# ------------------------------------------------------------------ админ-доступ

@web_bp.before_request
def _require_admin():
    user = current_app.config.get('ADMIN_USER') or ''
    password = current_app.config.get('ADMIN_PASSWORD') or ''
    if not password:
        return None  # пароль не задан — админка открыта (только для локальной отладки)
    auth = request.authorization
    if auth and hmac.compare_digest(auth.username or '', user) and \
            hmac.compare_digest(auth.password or '', password):
        return None
    return Response('Требуется авторизация', 401,
                    {'WWW-Authenticate': 'Basic realm="Timeline Notifier"'})


# ------------------------------------------------------------------ утилиты

def _enrich(item: WorkItem) -> dict:
    deadline_dt = None
    if item.deadline:
        try:
            deadline_dt = datetime.strptime(item.deadline, '%d.%m.%Y %H:%M')
        except ValueError:
            pass

    status = item.status or ''
    is_overdue = (
        deadline_dt is not None
        and deadline_dt < datetime.now()
        and status in NOT_SENT_STATUSES
    )

    return {
        'discipline': item.discipline,
        'deadline': item.deadline,
        'status': status or 'Не отправлена',
        'last_updated_at': item.last_updated_at,
        'is_overdue': is_overdue,
    }


def _global_interval(setting) -> int:
    return (setting.check_interval if setting and setting.check_interval
            else current_app.config['DEFAULT_CHECK_INTERVAL'])


# ------------------------------------------------------------------- страницы

@web_bp.route('/')
def index():
    setting = Setting.query.first() or Setting()
    users = User.query.order_by(User.created_at.desc()).all()
    default_interval = _global_interval(setting)

    rows = []
    for u in users:
        works = WorkItem.query.filter_by(user_id=u.id).all()
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

    setting.telegram_bot_token = request.form.get('telegram_bot_token', '').strip()
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
    result = check_user(current_app._get_current_object(), user.id,
                        notify=False, force=True)
    flash({'ok': 'Проверка выполнена (вход в Timeline прошёл)',
           'failed': 'Проверка не удалась — смотри ошибку в карточке',
           'skipped': 'Проверка пропущена (только что проверяли)',
           'unregistered': 'Пользователь ещё не прислал логин/пароль',
           }.get(result, result), 'info' if result == 'ok' else 'error')
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
