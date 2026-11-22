"""Persistent planner delivery; network sync and deadline checks are independent."""
import json
import logging
from datetime import timedelta

from app.models import db, User, WorkItem, PlannerEvent, utc_now
from app.bot.planner import active, now_local, deadline, unfinished, esc, button, keyboard

logger = logging.getLogger(__name__)


def schedule_delta(previous, current):
    """Match occurrences per subject/day; report actual old and new lesson fields."""
    from collections import defaultdict
    def flatten(days):
        result = defaultdict(list)
        for day in days:
            for lesson in day.get('lessons', []):
                key = (day.get('day', ''), lesson.get('subject', ''), lesson.get('subgroup', ''))
                result[key].append(lesson)
        for values in result.values():
            values.sort(key=lambda l: (l.get('time', ''), l.get('classroom', '')))
        return result
    old, new = flatten(previous), flatten(current)
    lines = []
    for key in sorted(old.keys() | new.keys()):
        before, after = old[key], new[key]
        for index in range(max(len(before), len(after))):
            a = before[index] if index < len(before) else None
            b = after[index] if index < len(after) else None
            if a == b:
                continue
            title = f'<b>{esc(key[0], 35)} · {esc(key[1], 80)}</b>'
            if a is None:
                lines.append(f'➕ {title}\nДобавлена: {esc(b.get("time"), 35)} · {esc(b.get("classroom"), 50)}')
            elif b is None:
                lines.append(f'➖ {title}\nУбрана из расписания: {esc(a.get("time"), 35)}')
            else:
                changes = [f'{label}: {esc(a.get(field), 50)} → <b>{esc(b.get(field), 50)}</b>'
                           for label, field in [('Время', 'time'), ('Аудитория', 'classroom'), ('Преподаватель', 'teacher')]
                           if a.get(field) != b.get(field)]
                lines.append('🔄 ' + title + '\n' + ('\n'.join(changes) or 'Изменились сведения о паре'))
    return '\n\n'.join(lines[:5]) + (f'\n\nЕщё изменений: {len(lines)-5}' if len(lines) > 5 else '')


def quiet(user, now=None):
    now = now or now_local()
    start, end = user.quiet_start or '23:00', user.quiet_end or '08:00'
    clock = now.strftime('%H:%M')
    return (start <= clock < end if start < end else clock >= start or clock < end) if start != end else False


def muted(user, item):
    return item and item.discipline in json.loads(user.muted_disciplines or '[]')


def record_change(user, item, changes, notify=True):
    """Called inside the same transaction as the work update."""
    text = f'🔄 <b>{esc(item.point_name or item.discipline, 100)}</b>\n'
    text += '\n'.join(f'{esc(label)}: {esc(old or "—", 60)} → <b>{esc(new or "—", 60)}</b>'
                      for label, old, new in changes)
    event = PlannerEvent(user_id=user.id, work_id=item.id, kind='change', text=text)
    if not (active(user) and notify and user.notify_enabled) or muted(user, item):
        event.sent_at = utc_now()  # history-only event
    db.session.add(event)


def record_new(user, item, notify=True):
    if not (active(user) and notify and user.notify_enabled and unfinished(item)) or muted(user, item):
        return
    if deadline(item) and deadline(item) < now_local() - timedelta(days=1):
        return  # An old work discovered during sync is not a new task for the student.
    db.session.add(PlannerEvent(user_id=user.id, work_id=item.id, kind='new',
        text=f'🆕 <b>Новое задание</b>\n{esc(item.discipline, 150)}\n{esc(item.point_name, 150)}\nСрок: {esc(item.deadline or "не указан")}'))


def process_planner_notifications(app):
    from app.bot.telegram_bot import get_sender, is_maintenance_mode
    from app.scheduler.jobs import _user_lock
    if is_maintenance_mode(app):
        return
    with app.app_context():
        sender = get_sender(app)
        if not sender:
            return
        users = User.query.filter_by(is_registered=True, is_active=True).all()
        for user in users:
            lock = _user_lock(user.id)
            if not lock.acquire(blocking=False):
                continue
            try:
                if active(user):
                    _process_user(sender, user)
                else:
                    _process_legacy(sender, user, app)
            except Exception:
                db.session.rollback()
                logger.exception('Planner delivery failed for user %s', user.id)
            finally:
                lock.release()
        # Keep recent delivered events for history, never discard pending deliveries.
        PlannerEvent.query.filter(PlannerEvent.sent_at.isnot(None),
                                  PlannerEvent.created_at < utc_now() - timedelta(days=90)).delete(synchronize_session=False)
        db.session.commit()


def _process_legacy(sender, user, app):
    """Keep the old message format; retry flags only advance on successful delivery."""
    if not user.notify_enabled:
        return
    from app.scheduler.jobs import _check_deadline_reminders
    from app.bot.telegram_bot import notify_deadline_reminders, notify_overdue
    items = WorkItem.query.filter_by(user_id=user.id).all()
    groups, overdue = _check_deadline_reminders(items, send=False, app=app)

    class Delivery:
        def __init__(self):
            self.app = app
            self.ok = True

        def send_message(self, *args, **kwargs):
            result = sender.send_message(*args, **kwargs)
            self.ok = self.ok and bool(result)
            return result

    delivery = Delivery()
    if any(groups.values()):
        notify_deadline_reminders(delivery, user.chat_id, groups, user=user)
    if overdue:
        notify_overdue(delivery, user.chat_id, overdue, user=user)
    if delivery.ok:
        _check_deadline_reminders(items, send=True, app=app)
        db.session.commit()


def _process_user(sender, user):
    now = now_local()
    if not user.notify_enabled:
        return
    pending = []
    for item in WorkItem.query.filter_by(user_id=user.id).all():
        if not unfinished(item):
            item.snooze_until = None
            continue
        if muted(user, item):
            continue
        due = deadline(item)
        hours_left = (due-now).total_seconds()/3600 if due else None
        sent = set(filter(None, (item.planner_reminded or '').split(',')))
        thresholds = sorted((int(h) for h in (user.reminder_hours or '').split(',') if h.isdigit()), reverse=True)
        eligible = [h for h in thresholds if hours_left is not None and 0 <= hours_left <= h]
        threshold = min(eligible) if eligible else None
        tag = 'overdue' if hours_left is not None and hours_left < 0 else str(threshold) if threshold is not None else None
        snoozed = item.snooze_until and item.snooze_until > now
        personal_due = item.snooze_until and item.snooze_until <= now
        if tag == 'overdue' and (hours_left < -24 or item.overdue_sent) and not personal_due:
            sent.add('overdue')
            item.planner_reminded = ','.join(sorted(sent))
            continue
        urgent = hours_left is not None and 0 <= hours_left <= 1
        if snoozed or (quiet(user, now) and not (urgent and user.urgent_at_night)):
            continue
        if not personal_due and (tag is None or tag in sent):
            continue
        title = '⏰ Личное напоминание' if personal_due else '🔴 Дедлайн прошёл' if tag == 'overdue' else f'⏳ До дедлайна ≤ {threshold} ч'
        from app.services import clean_discipline_name
        text = f'<b>{title}</b>\n{esc(clean_discipline_name(item.discipline), 90)} · {esc(item.point_name, 100)}\nСрок: {esc(item.deadline or "не указан")} '
        pending.append((bool(personal_due or urgent), item, tag, threshold, sent, text))
    db.session.commit()  # Persist silent suppression even if there is nothing to deliver.
    events = []
    if not quiet(user, now):
        events = PlannerEvent.query.filter_by(user_id=user.id, sent_at=None).filter(
            PlannerEvent.due_at <= utc_now()).order_by(PlannerEvent.id).limit(30).all()
        deliverable = []
        for event in events:
            item = db.session.get(WorkItem, event.work_id) if event.work_id else None
            stale = event.created_at < utc_now() - timedelta(days=1)
            stale_new = event.kind == 'new' and item and deadline(item) and deadline(item) < now - timedelta(days=1)
            if muted(user, item) or stale or stale_new:
                event.sent_at = utc_now()
                continue
            deliverable.append(event)
        events = deliverable
        db.session.commit()
    cooling = user.planner_last_notice_at and utc_now() - user.planner_last_notice_at < timedelta(minutes=15)
    pending.sort(key=lambda p: (not p[0], deadline(p[1]) or now, p[1].id))
    if cooling:
        pending = [p for p in pending if p[0]]
        events = []
    if not pending and not events:
        return
    lines = ['📨 <b>Обновления Планера</b>', '']
    selected, selected_events, rows = [], [], []
    for entry in pending[:5]:
        lines.append(entry[5])
        lines.append('')
        item = entry[1]
        rows.append([button(f'📄 {item.point_name or "Задание"}'[:45], f'p|card|{item.id}'),
                     button('⏰ Позже', f'p|snooze|{item.id}')])
        selected.append(entry)
    for event in events[:max(0, 5-len(selected))]:
        if len('\n'.join(lines)) + len(event.text) > 3500:
            break
        lines.extend([event.text, ''])
        selected_events.append(event)
        if event.work_id:
            rows.append([button('📄 Открыть задание', f'p|card|{event.work_id}')])
    rows.append([button('📋 Актуальные задания', 'p|list|0|open')])
    ok = sender.send_message(user.chat_id, '\n'.join(lines), reply_markup=keyboard(rows))
    if ok:
        user.planner_last_notice_at = utc_now()
        for _, item, tag, threshold, sent, _ in selected:
            if item.snooze_until and item.snooze_until <= now:
                item.snooze_until = None
            if tag:
                sent.add(tag)
                if tag == 'overdue':
                    sent.update(str(h) for h in thresholds)
                elif threshold is not None:
                    sent.update(str(h) for h in thresholds if h >= threshold)
                item.planner_reminded = ','.join(sorted(sent))
        for event in selected_events:
            event.sent_at = utc_now()
    else:
        for event in selected_events:
            event.attempts += 1
            event.due_at = utc_now() + timedelta(seconds=min(3600, 60 * 2**min(event.attempts, 6)))
    db.session.commit()
