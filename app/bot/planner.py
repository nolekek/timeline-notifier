"""Opt-in planner UI. Legacy and femboy interfaces keep their existing handlers."""
import html
import json
from datetime import datetime, timedelta, timezone

from app.models import db, User, WorkItem, PlannerEvent, utc_now
from app.services import NOT_SENT_STATUSES, clean_discipline_name

PROGRESS = {'todo': 'Не начато', 'doing': 'Делаю', 'ready': 'Готово к сдаче'}
MENU = [['📋 Мои задания', '🗓 Моя неделя'], ['📅 Расписание', '📊 Баллы'],
        ['🔄 Проверить', '⚙️ Настройки']]


def now_local():
    # Timeline deadlines and the existing deployment use Moscow time, UTC+3.
    return datetime.now(timezone(timedelta(hours=3))).replace(tzinfo=None)


def active(user):
    if isinstance(user, dict):
        return bool(user.get('theme_new') and user.get('new_unlocked'))
    return bool(getattr(user, 'theme_new', False) and getattr(user, 'new_unlocked', False))


def esc(value, limit=200):
    raw = str(value or '')
    result = []
    length = 0
    for char in raw[:limit]:
        escaped = html.escape(char)
        if length + len(escaped) > limit:
            break
        result.append(escaped)
        length += len(escaped)
    return ''.join(result)


def button(text, data):
    return {'text': text, 'callback_data': data}


def keyboard(rows):
    return {'inline_keyboard': rows}


def reply_keyboard():
    return {'keyboard': [[{'text': s} for s in row] for row in MENU],
            'resize_keyboard': True, 'is_persistent': True,
            'input_field_placeholder': 'Задания, неделя или настройки'}


def themes(user):
    selected = 'Планер' if active(user) else ('Фембой' if user.theme_femboy else 'Классическая')
    rows = [[button('Классическая' + (' ✓' if selected == 'Классическая' else ''), 'theme|legacy')]]
    if user.femboy_unlocked:
        rows.append([button('Фембой' + (' ✓' if selected == 'Фембой' else ''), 'theme|femboy')])
    if user.new_unlocked:
        rows.append([button('🧪 Планер' + (' ✓' if selected == 'Планер' else ''), 'theme|new')])
    rows.append([button('← Профиль', 'profile')])
    return (f'🎨 <b>Темы</b>\n\nСейчас: {selected}.\n'
            'Классическая тема остаётся по умолчанию. Выбор сохраняется только для тебя.', keyboard(rows))


def deadline(item):
    try:
        return datetime.strptime(item.deadline or '', '%d.%m.%Y %H:%M')
    except ValueError:
        return None


def unfinished(item):
    return (item.status or '') in NOT_SENT_STATUSES


def items_for(user):
    return sorted(WorkItem.query.filter_by(user_id=user.id).all(),
                  key=lambda it: (not unfinished(it), deadline(it) or datetime.max, it.id))


def list_view(user, page=0, mode='open', offset=0):
    page = max(0, min(page, 10000))
    items = items_for(user)
    if mode == 'week':
        start = now_local().date() - timedelta(days=now_local().weekday()) + timedelta(weeks=offset)
        items = [it for it in items if deadline(it) and start <= deadline(it).date() < start + timedelta(days=7)]
    elif mode == 'open':
        items = [it for it in items if unfinished(it)]
    elif mode == 'doing':
        items = [it for it in items if it.personal_progress == 'doing' and unfinished(it)]
    pages = max(1, (len(items) + 3) // 4)
    page = min(page, pages - 1)
    lines = [f'📋 <b>Мои задания</b> · {len(items)} · {page + 1}/{pages}', '']
    rows = []
    for it in items[page * 4:page * 4 + 4]:
        dt = deadline(it)
        icon = '🔴' if dt and dt < now_local() and unfinished(it) else '📄'
        lines.extend([f'{icon} <b>{esc(clean_discipline_name(it.discipline), 100)}</b>',
                      esc(it.point_name or 'Задание', 120),
                      f'Срок: {esc(it.deadline or "не указан")} · {esc(it.status or "Не отправлена")}',
                      f'Мой прогресс: {PROGRESS.get(it.personal_progress, PROGRESS["todo"])}', ''])
        rows.append([button(f'📄 {str(it.point_name or clean_discipline_name(it.discipline))[:45]}', f'p|card|{it.id}')])
    if not items:
        lines.append('Здесь пока нет заданий. Данные обновляются каждые 6 часов; можно нажать «Проверить».')
    rows.append([button('◀️', f'p|list|{page-1}|{mode}|{offset}'), button('▶️', f'p|list|{page+1}|{mode}|{offset}')])
    rows.append([button('Несданные', 'p|list|0|open'), button('Делаю', 'p|list|0|doing'), button('Все', 'p|list|0|all')])
    rows.append([button('🗓 Неделя', f'p|week|{offset}'), button('🏠 Меню', 'p|home')])
    return '\n'.join(lines), keyboard(rows)


def week_view(user, offset=0):
    from app.parser.schedule import get_group_schedule
    offset = max(-12, min(12, offset))
    today = now_local().date()
    start = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
    end = start + timedelta(days=6)
    try:
        schedule = get_group_schedule(user.current_group)
    except Exception:
        schedule = []
    by_date = {d['date']: d['lessons'] for d in schedule if d.get('date')}
    works = items_for(user)
    lines = [f'🗓 <b>{start:%d.%m}–{end:%d.%m.%Y}</b> · {esc(user.current_group)}',
             'Время: МСК · статусы из последней синхронизации', '']
    lesson_count = task_count = 0
    days = ('Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс')
    for n in range(7):
        day = start + timedelta(days=n)
        lessons = by_date.get(day, [])
        tasks = [it for it in works if deadline(it) and deadline(it).date() == day and unfinished(it)]
        lesson_count += len(lessons)
        task_count += len(tasks)
        lines.append(f'<b>{days[n]}, {day:%d.%m}' + (' · сегодня' if day == today else '') + '</b>')
        if lessons:
            lines.append(f'🎓 Пар: {len(lessons)} · {esc(lessons[0].get("time"), 35)} — {esc(lessons[-1].get("time"), 35)}')
        else:
            lines.append('Пар в загруженном расписании нет')
        for it in tasks[:3]:
            lines.append(f'⏳ {deadline(it):%H:%M} · {esc(clean_discipline_name(it.discipline), 65)} · {esc(it.point_name, 65)}')
        if len(tasks) > 3:
            lines.append(f'Ещё заданий: {len(tasks)-3}')
        if not tasks:
            lines.append('Дедлайнов нет')
        lines.append('')
    lines.append(f'За неделю: {lesson_count} пар · {task_count} заданий к сдаче')
    if not schedule:
        lines.append('⚠️ Расписание не получено. Это не подтверждение отсутствия пар.')
    return '\n'.join(lines), keyboard([
        [button('◀️ Предыдущая', f'p|week|{offset-1}'), button('Следующая ▶️', f'p|week|{offset+1}')],
        [button('📋 Задания недели', f'p|list|0|week|{offset}'), button('Эта неделя', 'p|week|0')],
        [button('🏠 Меню', 'p|home')]])


def card_view(user, item):
    lines = [f'📄 <b>{esc(item.point_name or "Задание", 180)}</b>', esc(clean_discipline_name(item.discipline), 150), '',
             f'📅 Сдать до: {esc(item.deadline or "не указан")}',
             f'🏫 Timeline: {esc(item.status or "Не отправлена")}',
             f'📝 Мой прогресс: {PROGRESS.get(item.personal_progress, PROGRESS["todo"])}']
    dt = deadline(item)
    if dt and unfinished(item):
        minutes = int((dt - now_local()).total_seconds() / 60)
        lines.append(f'⏳ Осталось: {max(0, minutes)//60} ч {max(0, minutes)%60} мин' if minutes >= 0 else '🔴 Дедлайн прошёл')
    if item.snooze_until:
        lines.append(f'⏰ Личное напоминание: {item.snooze_until:%d.%m %H:%M} МСК')
    if item.points:
        lines.append(f'🎯 Баллы: {esc(item.points)}')
    lines.extend(['', '<b>Задание преподавателя</b>', esc(item.control_text or 'Текст не заполнен. Материалы смотри в Timeline.', 1250)])
    if item.teacher_note:
        lines.extend(['', '<b>Рецензия</b>', esc(item.teacher_note, 350)])
    if item.personal_note:
        lines.extend(['', '<b>Моя заметка</b>', esc(item.personal_note, 500)])
    if item.unavailability_reason:
        lines.append(f'⚠️ {esc(item.unavailability_reason, 200)}')
    lines.append('\nЛичный прогресс не меняет статус в Timeline и не отменяет напоминания.')
    i = item.id
    return '\n'.join(lines), keyboard([
        [button('📝 Прогресс', f'p|progress|{i}'), button('🗒 Моя заметка', f'p|note|{i}')],
        [button('⏰ Напомнить позже', f'p|snooze|{i}'), button('🕘 История', f'p|history|{i}')],
        [{'text': '🔗 Открыть в Timeline', 'url': 'https://timeline.rsvpu.ru/StudentWork/Send'}],
        [button('← К заданиям', 'p|list|0|open')]])


def settings_view(user):
    muted = json.loads(user.muted_disciplines or '[]')
    quiet = f'{user.quiet_start}–{user.quiet_end}' if user.quiet_start != user.quiet_end else 'выключены'
    lines = ['⚙️ <b>Настройки Планера</b>', '', f'🔔 Уведомления: {"включены" if user.notify_enabled else "выключены"}',
             f'⏳ Пороги: {user.reminder_hours or "выключены"} ч', f'🌙 Тихие часы: {quiet} (МСК)',
             f'🔥 Часовые напоминания ночью: {"да" if user.urgent_at_night else "нет"}',
             f'☀️ Дайджест: {user.digest_notify_time} · {"вкл" if user.digest_notify else "выкл"}',
             f'📅 Расписание: {user.schedule_notify_time} · {"вкл" if user.schedule_notify else "выкл"}',
             f'📚 Отключено предметов: {len(muted)}', '',
             'Timeline обновляется каждые 6 часов. Напоминания по сохранённым срокам проверяются каждую минуту.']
    return '\n'.join(lines), keyboard([
        [button('🔔 Вкл/выкл', 'p|toggle|notify_enabled'), button('⏳ Пороги', 'p|thresholds')],
        [button('🌙 Тихие часы', 'p|input|quiet'), button('🔥 Срочные ночью', 'p|toggle|urgent_at_night')],
        [button('☀️ Дайджест вкл/выкл', 'p|toggle|digest_notify'), button('Время', 'p|input|digest')],
        [button('📅 Расписание вкл/выкл', 'p|toggle|schedule_notify'), button('Время', 'p|input|schedule')],
        [button('📚 Предметы', 'p|muted|0'), button('🎨 Темы', 'themes')],
        [button('👤 Профиль', 'profile'), button('🏠 Меню', 'p|home')]])


def home_view(user):
    items = [it for it in items_for(user) if unfinished(it)]
    overdue = sum(bool(deadline(it) and deadline(it) < now_local()) for it in items)
    doing = sum(it.personal_progress == 'doing' for it in items)
    return (f'🧭 <b>Планер</b>\n\nК сдаче: {len(items)} · Просрочено: {overdue} · Делаю: {doing}\n'
            'Выбери раздел. Прогресс и заметки видны только тебе.\nВремя дедлайнов и настроек — МСК.',
            keyboard([[button('📋 Задания', 'p|list|0|open'), button('🗓 Моя неделя', 'p|week|0')],
                      [button('⚙️ Настройки', 'p|settings'), button('🎨 Темы', 'themes')]]))


def handle_message(bot, user, chat_id, text):
    command = text.split()[0].lower().split('@')[0] if text else ''
    if command == '/new':
        user.new_unlocked = True
        db.session.commit()
        body, kb = themes(user)
        bot.send_message(chat_id, '🧪 <b>Планер разблокирован.</b> Включи его кнопкой ниже.\n\n' + body, reply_markup=kb)
        return True
    if command == '/themes' or text == '🎨 Темы':
        body, kb = themes(user)
        bot.send_message(chat_id, body, reply_markup=kb)
        return True
    if not active(user) or not user.is_registered or user.state != 'ready':
        return False
    if user.planner_input and text not in {s for row in MENU for s in row} and (not command.startswith('/') or command == '/cancel'):
        return save_input(bot, user, chat_id, text)
    if command in ('/cancel', '/login', '/unlink', '/logout', '/start'):
        user.planner_input = None
        db.session.commit()
        return False
    view = None
    if text == '🗓 Моя неделя' or command == '/week':
        view = week_view(user)
    elif text == '📋 Мои задания' or command == '/status':
        view = list_view(user)
    elif text == '⚙️ Настройки' or command == '/settings':
        view = settings_view(user)
    elif command == '/menu':
        bot.send_message(chat_id, '🧭 Планер · меню обновлено', reply_markup=reply_keyboard())
        view = home_view(user)
    elif text == '📊 Баллы':
        bot._cmd_scores(user, chat_id)
        return True
    elif command in ('/task', '/work', '/задание', '/работа'):
        parts = text.split(maxsplit=1)
        if len(parts) > 1:
            it = WorkItem.query.filter_by(user_id=user.id, external_id=parts[1]).first()
            if it:
                view = card_view(user, it)
    if view:
        bot.send_message(chat_id, view[0], reply_markup=view[1])
        return True
    return False


def save_input(bot, user, chat_id, text):
    action = user.planner_input
    if text.lower() == '/cancel':
        user.planner_input = None
        db.session.commit()
        bot.send_message(chat_id, 'Отменено.', reply_markup=reply_keyboard())
        return True
    try:
        if action.startswith('note:'):
            it = WorkItem.query.filter_by(id=int(action.split(':')[1]), user_id=user.id).first()
            if not it:
                raise ValueError('Задание больше не доступно; /cancel для отмены.')
            if len(text) > 500:
                raise ValueError('Заметка — до 500 символов.')
            it.personal_note = '' if text == '-' else text
        elif action == 'quiet':
            parts = text.split('-')
            if len(parts) != 2:
                raise ValueError('Формат: 23:00-08:00. Для отключения: 00:00-00:00.')
            user.quiet_start, user.quiet_end = [parse_time(s.strip()) for s in parts]
        elif action in ('digest', 'schedule'):
            setattr(user, f'{action}_notify_time', parse_time(text))
        else:
            raise ValueError('Действие устарело; /cancel для отмены.')
    except ValueError as ex:
        bot.send_message(chat_id, esc(ex, 250))
        return True
    user.planner_input = None
    db.session.commit()
    view = card_view(user, it) if action.startswith('note:') else settings_view(user)
    bot.send_message(chat_id, '✅ Сохранено\n\n' + view[0], reply_markup=view[1])
    return True


def parse_time(value):
    try:
        return datetime.strptime(value.strip(), '%H:%M').strftime('%H:%M')
    except ValueError:
        raise ValueError('Введи время ЧЧ:ММ, например 08:00 (МСК).') from None


def handle_callback(bot, user, chat_id, message_id, cb_id, data):
    if data == 'themes' or data.startswith('theme|'):
        if data.startswith('theme|'):
            name = data.split('|')[1]
            allowed = name == 'legacy' or (name == 'new' and user.new_unlocked) or (name == 'femboy' and user.femboy_unlocked)
            if not allowed:
                bot.answer_callback(cb_id, 'Тема недоступна')
                return True
            user.theme_new = name == 'new'
            user.theme_femboy = name == 'femboy'
            user.planner_input = None
            db.session.commit()
            bot.send_message(chat_id, 'Меню обновлено', reply_markup=bot._main_keyboard(user))
        bot.answer_callback(cb_id)
        view = themes(user)
        bot.edit_message(chat_id, message_id, view[0], view[1])
        return True
    if not data.startswith('p|'):
        return False
    if not active(user) or not user.is_registered:
        bot.answer_callback(cb_id, 'Включи Планер в «Темах» и войди через /login')
        return True
    parts = data.split('|')
    action = parts[1]
    try:
        view = None
        if action == 'home':
            view = home_view(user)
        elif action == 'list':
            mode = parts[3] if len(parts) > 3 and parts[3] in ('open', 'all', 'doing', 'week') else 'open'
            view = list_view(user, int(parts[2]), mode, max(-12, min(12, int(parts[4]))) if len(parts) > 4 else 0)
        elif action == 'week':
            view = week_view(user, int(parts[2]))
        elif action == 'settings':
            view = settings_view(user)
        elif action == 'toggle':
            if parts[2] not in ('notify_enabled', 'urgent_at_night', 'digest_notify', 'schedule_notify'):
                raise ValueError('Неизвестная настройка')
            setattr(user, parts[2], not getattr(user, parts[2]))
            db.session.commit()
            view = settings_view(user)
        elif action == 'input':
            if parts[2] not in ('quiet', 'digest', 'schedule'):
                raise ValueError('Неизвестная настройка')
            user.planner_input = parts[2]
            db.session.commit()
            view = ('Пришли тихие часы: 23:00-08:00 (МСК).' if parts[2] == 'quiet' else 'Пришли время: 08:00 (МСК).', keyboard([[button('Отмена', 'p|cancel')]]))
        elif action == 'cancel':
            user.planner_input = None
            db.session.commit()
            view = settings_view(user)
        elif action == 'thresholds':
            view = ('⏳ <b>Напоминания до дедлайна</b>', keyboard([
                [button('24 / 12 / 1 ч', 'p|hours|24,12,1')], [button('48 / 24 / 1 ч', 'p|hours|48,24,1')],
                [button('Только за час', 'p|hours|1'), button('Выключить', 'p|hours|off')],
                [button('← Настройки', 'p|settings')]]))
        elif action == 'hours':
            if parts[2] not in ('24,12,1', '48,24,1', '1', 'off'):
                raise ValueError('Неизвестные пороги')
            user.reminder_hours = '' if parts[2] == 'off' else parts[2]
            db.session.commit()
            view = settings_view(user)
        elif action in ('muted', 'mute'):
            discs = sorted({it.discipline for it in items_for(user)})
            muted = json.loads(user.muted_disciplines or '[]')
            page = max(0, min(int(parts[2]), max(0, (len(discs)-1)//6)))
            if action == 'mute':
                # Work ID is stable even if the list changes between clicks.
                it = WorkItem.query.filter_by(user_id=user.id, id=int(parts[3])).first()
                if not it:
                    raise ValueError('Задание больше не доступно')
                if it.discipline in muted:
                    muted.remove(it.discipline)
                else:
                    muted.append(it.discipline)
                user.muted_disciplines = json.dumps(muted, ensure_ascii=False)
                db.session.commit()
            rows = []
            for disc in discs[page*6:page*6+6]:
                it = WorkItem.query.filter_by(user_id=user.id, discipline=disc).first()
                rows.append([button(('🔕 ' if disc in muted else '🔔 ') + clean_discipline_name(disc)[:40], f'p|mute|{page}|{it.id}')])
            rows.extend([[button('◀️', f'p|muted|{page-1}'), button('▶️', f'p|muted|{page+1}')], [button('← Настройки', 'p|settings')]])
            view = ('📚 <b>Уведомления по предметам</b>\nНажми предмет для переключения. Задания остаются в списке.', keyboard(rows))
        elif action in ('card', 'progress', 'setprogress', 'note', 'snooze', 'setsnooze', 'history'):
            it = WorkItem.query.filter_by(id=int(parts[2]), user_id=user.id).first()
            if not it:
                raise ValueError('Задание больше не доступно')
            i = it.id
            if action == 'progress':
                view = ('📝 <b>Мой прогресс</b>\nОфициальный статус Timeline не изменится.', keyboard([
                    [button(label, f'p|setprogress|{i}|{key}')] for key, label in PROGRESS.items()] + [[button('← Задание', f'p|card|{i}')]]))
            elif action == 'setprogress':
                if parts[3] not in PROGRESS:
                    raise ValueError('Неизвестный прогресс')
                it.personal_progress = parts[3]
                db.session.commit()
                view = card_view(user, it)
            elif action == 'note':
                user.planner_input = f'note:{i}'
                db.session.commit()
                view = ('Пришли личную заметку (до 500 символов). «-» удалит заметку. /cancel — отмена.', keyboard([[button('← Задание', f'p|card|{i}')]]))
            elif action == 'snooze':
                view = ('⏰ <b>Напомнить позже</b> · МСК\nПовторное напоминание заменяет предыдущее.', keyboard([
                    [button('Через 30 мин', f'p|setsnooze|{i}|30'), button('Через час', f'p|setsnooze|{i}|60')],
                    [button('Вечером, 19:00', f'p|setsnooze|{i}|evening'), button('Завтра, 09:00', f'p|setsnooze|{i}|tomorrow')],
                    [button('Отменить напоминание', f'p|setsnooze|{i}|off')], [button('← Задание', f'p|card|{i}')]]))
            elif action == 'setsnooze':
                now = now_local()
                option = parts[3]
                if option in ('30', '60'):
                    it.snooze_until = now + timedelta(minutes=int(option))
                elif option in ('evening', 'tomorrow'):
                    target = now.replace(hour=19 if option == 'evening' else 9, minute=0, second=0, microsecond=0)
                    it.snooze_until = target + timedelta(days=1) if option == 'tomorrow' or target <= now else target
                elif option == 'off':
                    it.snooze_until = None
                else:
                    raise ValueError('Неизвестное время')
                db.session.commit()
                view = card_view(user, it)
            elif action == 'history':
                events = PlannerEvent.query.filter_by(user_id=user.id, work_id=i, kind='change').order_by(PlannerEvent.id.desc()).limit(3).all()
                view = ('🕘 <b>История изменений</b>\n\n' + ('\n\n'.join(e.text for e in events) or 'Изменений после обновления пока нет.'), keyboard([[button('← Задание', f'p|card|{i}')]]))
            else:
                user.planner_input = None
                db.session.commit()
                view = card_view(user, it)
        else:
            raise ValueError('Неизвестное действие')
        bot.answer_callback(cb_id)
        if view:
            bot.edit_message(chat_id, message_id, view[0], view[1])
    except (ValueError, IndexError):
        bot.answer_callback(cb_id, 'Кнопка устарела. Открой /menu заново.', show_alert=True)
    return True
