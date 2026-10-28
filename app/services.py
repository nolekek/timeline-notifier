"""Общая доменная логика про работы — используется и ботом, и админкой.

Живёт отдельно, чтобы бот не импортировал веб-роуты (и наоборот).
"""
from datetime import datetime, timedelta, timezone
import html as html_mod
import re
from sqlalchemy import event
from sqlalchemy.orm import object_session

from app.models import db, Setting, User, WorkItem

# Статусы, означающие «студент ещё не отправил работу»
NOT_SENT_STATUSES = ('', 'Не отправлена', 'не отправлена')

# Статусы с полным иммунитетом к просрочке (R1, AC 54)
COMPLETED_STATUSES = {'Зачтена', 'Проверена', 'Отправлена'}


# Автоматическое повторное прикрепление отсоединённых объектов к db.session при изменении полей
def _register_auto_reattach():
    for model in (User, Setting, WorkItem):
        for col in model.__table__.columns:
            prop = getattr(model, col.name, None)
            if prop is not None:
                @event.listens_for(prop, 'set', propagate=True)
                def _auto_reattach_listener(target, value, oldvalue, initiator):
                    if object_session(target) is None and getattr(target, 'id', None) is not None:
                        try:
                            db.session.add(target)
                        except Exception:
                            pass


_register_auto_reattach()


def _get_item_field(item, field: str, default=None):
    """Извлекает поле как из объекта (модель, SimpleNamespace), так и из dict."""
    if isinstance(item, dict):
        return item.get(field, default)
    return getattr(item, field, default)


def _is_flag_true(item, *names) -> bool:
    """Проверяет истинность флагов проверки (PointAccepted, WorkChecked)."""
    for name in names:
        val = _get_item_field(item, name, None)
        if val is True:
            return True
        if isinstance(val, str) and val.strip().lower() in ('true', '1'):
            return True
        if isinstance(val, (int, float)) and val == 1 and not isinstance(val, bool):
            return True
    return False


def is_work_overdue(deadline_or_item=None,
                    status: str | None = None,
                    point_accepted: bool = False,
                    work_checked: bool = False,
                    now: datetime | None = None,
                    **kwargs) -> bool:
    """Определяет, просрочена ли работа (R1, AC 54).

    Работа НИКОГДА не считается просроченной (is_overdue = False), если:
    - PointAccepted == True или WorkChecked == True
    - Статус не пустой и не 'Не отправлена' (например: 'Зачтена', 'Проверена', 'Отправлена')
    - Дедлайн не задан или находится в будущем
    """
    if deadline_or_item is not None and not isinstance(deadline_or_item, (str, datetime)):
        # Вызов вида is_work_overdue(item)
        item = deadline_or_item
        deadline = _get_item_field(item, 'deadline')
        raw_status = _get_item_field(item, 'status')
        pa = _is_flag_true(item, 'PointAccepted', 'point_accepted') or point_accepted
        wc = _is_flag_true(item, 'WorkChecked', 'work_checked') or work_checked
    else:
        deadline = deadline_or_item if deadline_or_item is not None else kwargs.get('deadline')
        raw_status = status if status is not None else kwargs.get('status')
        pa = point_accepted or kwargs.get('point_accepted', False)
        wc = work_checked or kwargs.get('work_checked', False)

    if pa or wc:
        return False

    clean_status = (raw_status or '').strip().lower()
    if clean_status and clean_status != 'не отправлена':
        return False

    deadline_dt = None
    if isinstance(deadline, datetime):
        deadline_dt = deadline
    elif deadline:
        try:
            deadline_dt = datetime.strptime(str(deadline).strip(), '%d.%m.%Y %H:%M')
        except (ValueError, TypeError):
            return False

    if deadline_dt is None:
        return False

    current_time = now or datetime.now()
    if current_time.tzinfo is not None and deadline_dt.tzinfo is None:
        current_time = current_time.replace(tzinfo=None)
    elif current_time.tzinfo is None and deadline_dt.tzinfo is not None:
        deadline_dt = deadline_dt.replace(tzinfo=None)

    return deadline_dt < current_time


def enrich_work_item(item) -> dict:
    """WorkItem -> словарь для отображения: статус, просроченность, сроки."""
    deadline = _get_item_field(item, 'deadline')
    raw_status = _get_item_field(item, 'status')
    point_accepted = _is_flag_true(item, 'PointAccepted', 'point_accepted')
    work_checked = _is_flag_true(item, 'WorkChecked', 'work_checked')

    is_overdue = is_work_overdue(
        deadline=deadline,
        status=raw_status,
        point_accepted=point_accepted,
        work_checked=work_checked,
    )

    status_str = (raw_status or '').strip()
    if not status_str:
        if point_accepted:
            display_status = 'Зачтена'
        elif work_checked:
            display_status = 'Проверена'
        else:
            display_status = 'Не отправлена'
    else:
        display_status = raw_status

    return {
        'id': _get_item_field(item, 'id'),
        'external_id': str(_get_item_field(item, 'external_id') or ''),
        'discipline': _get_item_field(item, 'discipline'),
        'deadline': deadline,
        'status': display_status,
        'last_updated_at': _get_item_field(item, 'last_updated_at'),
        'is_overdue': is_overdue,
        'point_name': _get_item_field(item, 'point_name') or '',
        'control_text': _get_item_field(item, 'control_text') or '',
        'points': _get_item_field(item, 'points') or '',
        'teacher_note': _get_item_field(item, 'teacher_note') or '',
        'unavailability_reason': _get_item_field(item, 'unavailability_reason') or '',
    }


def format_diagnostic_block(module: str, description: str, attempts: str = "3/3", tz_offset_hours: int = 3) -> str:
    """Генерирует моноширинный блок диагностики для отчётов об ошибках в формате HTML.

    Пример:
    <code>Модуль: TimelineAuth | Ошибка: Сбой авторизации | Попытки: 3/3 | Время: 15:30:00 MSK</code>
    """
    now_utc = datetime.now(timezone.utc)
    now_msk = now_utc + timedelta(hours=tz_offset_hours)
    time_str = now_msk.strftime('%H:%M:%S')

    mod_str = html_mod.escape(str(module or ''))
    desc_str = html_mod.escape(str(description or ''))
    att_str = html_mod.escape(str(attempts or ''))

    return f"<code>Модуль: {mod_str} | Ошибка: {desc_str} | Попытки: {att_str} | Время: {time_str} MSK</code>"


# Поддерживаемые расширения файлов заданий и учебных материалов
KNOWN_FILE_EXTENSIONS = {
    'pdf', 'doc', 'docx', 'odt', 'rtf', 'txt', 'tex',
    'xls', 'xlsx', 'csv', 'ods',
    'ppt', 'pptx', 'odp',
    'zip', 'rar', '7z', 'tar', 'gz',
    'py', 'c', 'cpp', 'cs', 'java', 'js', 'html', 'css', 'sql',
    'png', 'jpg', 'jpeg'
}

_FILE_REGEX = re.compile(
    r'\b([a-zA-Z0-9_\-\.А-Яа-яЁё]+?\.(?:' + '|'.join(KNOWN_FILE_EXTENSIONS) + r'))\b',
    re.IGNORECASE
)
_URL_REGEX = re.compile(r'(https?://[^\s<>"\'\)]+)', re.IGNORECASE)


def extract_file_attachments(text: str | None) -> list[str]:
    """Извлекает названия файлов с расширениями из текста задания преподавателя или рецензии.

    Позволяет студентам сразу видеть имена прикреплённых или требуемых файлов
    (например, 'LR_01.docx', 'metodichka.pdf', 'dataset.zip').
    """
    if not text:
        return []
    # Удаляем URL, чтобы части адресов не определялись как файлы
    clean = _URL_REGEX.sub('', text)
    matches = _FILE_REGEX.findall(clean)
    seen = set()
    result = []
    for m in matches:
        cleaned = m.strip('.,;:-\"\' \t\r\n')
        if '.' in cleaned:
            ext = cleaned.rsplit('.', 1)[-1].lower()
            if ext in KNOWN_FILE_EXTENSIONS:
                if cleaned.lower() not in seen:
                    seen.add(cleaned.lower())
                    result.append(cleaned)
    return result


def extract_urls(text: str | None) -> list[str]:
    """Извлекает внешние веб-ссылки (диски, облака, сайты) из текста задания."""
    if not text:
        return []
    matches = _URL_REGEX.findall(text)
    seen = set()
    result = []
    for u in matches:
        cleaned = u.rstrip('.,;:) \t\r\n')
        if cleaned.lower() not in seen:
            seen.add(cleaned.lower())
            result.append(cleaned)
    return result


