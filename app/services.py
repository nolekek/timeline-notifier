"""Общая доменная логика про работы — используется и ботом, и админкой.

Живёт отдельно, чтобы бот не импортировал веб-роуты (и наоборот).
"""
from datetime import datetime

# Статусы, означающие «студент ещё не отправил работу»
NOT_SENT_STATUSES = ('', 'Не отправлена')


def enrich_work_item(item) -> dict:
    """WorkItem -> словарь для отображения: статус, просроченность, сроки."""
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
