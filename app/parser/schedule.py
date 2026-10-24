"""Парсер и сервис расписания занятий УрГПУ (uspu.ru).

Источник: https://uspu.ru/education/eios/schedule/?group_name=...
AJAX эндпоинт: https://uspu.ru/ajax/rasp.php
Поддерживает:
- Динамическую загрузку расписания для групп (по умолчанию МКТ-2531);
- Кэширование в памяти с TTL (10 мин), чтобы не спамить сайт при частых кликах;
- Принудительное обновление по кнопке;
- Умный выбор: сегодня, завтра, текущая неделя, ближайший учебный день;
- Парсинг аудиторий, преподавателей, дисциплин, подгрупп.
"""
import html as html_mod
import logging
import re
import time
import urllib.parse
from datetime import date, datetime, timedelta
from typing import Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

SCHEDULE_AJAX_URL = 'https://uspu.ru/ajax/rasp.php'
SCHEDULE_MAIN_URL = 'https://uspu.ru/education/eios/schedule/'

RU_MONTHS = {
    'января': 1, 'январь': 1,
    'февраля': 2, 'февраль': 2,
    'марта': 3, 'март': 3,
    'апреля': 4, 'апрель': 4,
    'мая': 5, 'май': 5,
    'июня': 6, 'июнь': 6,
    'июля': 7, 'июль': 7,
    'августа': 8, 'август': 8,
    'сентября': 9, 'сентябрь': 9,
    'октября': 10, 'октябрь': 10,
    'ноября': 11, 'ноябрь': 11,
    'декабря': 12, 'декабрь': 12,
}

RU_WEEKDAYS = {
    0: 'Понедельник',
    1: 'Вторник',
    2: 'Среда',
    3: 'Четверг',
    4: 'Пятница',
    5: 'Суббота',
    6: 'Воскресенье',
}

# Кэш в памяти: {key: (timestamp, data)}
_SCHEDULE_CACHE: dict[str, tuple[float, list[dict]]] = {}
_GROUPS_CACHE: tuple[float, list[str]] = (0.0, [])
CACHE_TTL_SECONDS = 600  # 10 минут


def _parse_ru_date(day_text: str, reference_date: Optional[date] = None) -> Optional[date]:
    """Преобразует строку вида '26 октября' в объект datetime.date с учетом года."""
    if not day_text:
        return None
    ref = reference_date or date.today()
    parts = day_text.strip().split()
    if len(parts) < 2:
        return None

    try:
        day_num = int(parts[0])
    except ValueError:
        return None

    month_name = parts[1].lower()
    month_num = RU_MONTHS.get(month_name)
    if not month_num:
        return None

    # Определение года: если учебный год начался осенью (ref.month >= 8),
    # а дата из января-июля (month_num <= 7), то это следующий календарный год.
    year = ref.year
    if ref.month >= 8 and month_num <= 7:
        year = ref.year + 1
    elif ref.month <= 7 and month_num >= 8:
        year = ref.year - 1

    try:
        return date(year, month_num, day_num)
    except ValueError:
        return None


def fetch_schedule_html(group_name: str, exam: bool = False, timeout: int = 15) -> str:
    """Запрашивает HTML расписания с uspu.ru через AJAX."""
    headers = {
        'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                       'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'),
        'X-Requested-With': 'XMLHttpRequest',
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
        'Referer': f"{SCHEDULE_MAIN_URL}?group_name={urllib.parse.quote(group_name)}",
    }
    payload = {'group_name': group_name}
    if exam:
        payload['exam_stud'] = 'true'

    resp = requests.post(SCHEDULE_AJAX_URL, data=payload, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def parse_schedule_html(html: str, reference_date: Optional[date] = None) -> list[dict]:
    """Разбирает HTML ответ от /ajax/rasp.php в структурированный список дней и пар."""
    soup = BeautifulSoup(html, 'html.parser')
    days = []

    for item in soup.find_all('div', class_='rasp-item'):
        day_span = item.find('span', class_='rasp-day')
        week_div = item.find('div', class_='rasp-week')
        day_title = day_span.text.strip() if day_span else ''
        week_short = week_div.text.strip() if week_div else ''

        parsed_date = _parse_ru_date(day_title, reference_date)

        lessons = []
        for para in item.find_all('div', class_='rasp-para'):
            time_span = para.find('span', class_='para-time')
            time_str = time_span.text.strip() if time_span else ''

            desc_div = para.find('div', class_='rasp-desc')
            if not desc_div:
                continue

            p_tag = desc_div.find('p')
            if not p_tag:
                continue

            span_tag = p_tag.find('span')
            span_text = span_tag.get_text('\n').strip() if span_tag else ''

            # Извлекаем название предмета (дочерний текст до <span>)
            if span_tag:
                span_tag.extract()
            subject = ' '.join(p_tag.get_text().split()).strip()

            # Разбор строк в span: аудитория, преподаватель, группа, подгруппа
            classroom = ''
            teacher = ''
            subgroup = ''

            for line in span_text.split('\n'):
                line_str = line.strip()
                if not line_str:
                    continue
                if line_str.startswith('Преподаватель:'):
                    teacher = line_str[len('Преподаватель:'):].strip()
                elif line_str.startswith('Группа:'):
                    pass
                elif 'подгруппа' in line_str.lower():
                    subgroup = line_str
                elif not classroom:
                    classroom = line_str

            lessons.append({
                'time': time_str,
                'subject': subject,
                'classroom': classroom,
                'teacher': teacher,
                'subgroup': subgroup,
            })

        days.append({
            'date': parsed_date,
            'day_title': day_title,
            'weekday_short': week_short,
            'lessons': lessons,
        })

    # Сортируем дни хронологически, если даты распарсились
    days.sort(key=lambda d: d['date'] if d['date'] else date.max)
    return days


def get_group_schedule(group_name: str, force_refresh: bool = False, exam: bool = False) -> list[dict]:
    """Возвращает структурированное расписание для группы с кэшированием."""
    cache_key = f"{group_name.strip().upper()}:{'exam' if exam else 'regular'}"
    now_ts = time.time()

    if not force_refresh and cache_key in _SCHEDULE_CACHE:
        cached_ts, data = _SCHEDULE_CACHE[cache_key]
        if now_ts - cached_ts < CACHE_TTL_SECONDS:
            return data

    try:
        html = fetch_schedule_html(group_name, exam=exam)
        data = parse_schedule_html(html)
        _SCHEDULE_CACHE[cache_key] = (now_ts, data)
        return data
    except Exception as e:
        logger.error(f"Ошибка получения расписания для {group_name}: {e}")
        # Если есть старый кэш — вернем его при ошибке сети
        if cache_key in _SCHEDULE_CACHE:
            return _SCHEDULE_CACHE[cache_key][1]
        raise


def get_available_groups(force_refresh: bool = False) -> list[str]:
    """Список всех групп с сайта uspu.ru (кэш на 24 часа).

    При недоступности сайта и пустом кэше возвращает [] — чтобы вызывающий код
    понимал «проверить не смогли», а не получал фейковый список из одной группы.
    """
    global _GROUPS_CACHE
    now_ts = time.time()
    if not force_refresh and _GROUPS_CACHE[1] and (now_ts - _GROUPS_CACHE[0] < 86400):
        return _GROUPS_CACHE[1]

    try:
        resp = requests.get(SCHEDULE_MAIN_URL, timeout=15, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36'
        })
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
        select = soup.find('select', {'name': 'group_name'})
        groups = []
        if select:
            for opt in select.find_all('option'):
                val = opt.get('value', '').strip()
                if val:
                    groups.append(val)
        if groups:
            _GROUPS_CACHE = (now_ts, groups)
            return groups
    except Exception as e:
        logger.error(f"Не удалось получить список групп: {e}")

    return _GROUPS_CACHE[1] or []


def _esc(s: str) -> str:
    """Экранирование текста для Telegram HTML: данные приходят с чужого сайта."""
    return html_mod.escape(str(s or ''), quote=False)


def format_day_schedule(day: dict, group_name: str, note: str = "") -> str:
    """Форматирует один день расписания в Telegram HTML (всё экранировано)."""
    d_obj: Optional[date] = day.get('date')
    if d_obj:
        weekday_full = RU_WEEKDAYS.get(d_obj.weekday(), day.get('weekday_short', ''))
        header_date = f"{day.get('day_title', '')} ({weekday_full})"
    else:
        header_date = f"{day.get('day_title', '')} ({day.get('weekday_short', '')})"

    lines = [
        f"<b>📅 Расписание — {_esc(group_name)}</b>",
        f"<b>{_esc(header_date)}</b>",
    ]
    if note:
        lines.append(f"<i>{_esc(note)}</i>")
    lines.append("─" * 22)

    lessons = day.get('lessons', [])
    if not lessons:
        lines.append("<i>Пар нет, можно отдыхать! 🎉</i>")
        return "\n".join(lines)

    for idx, l in enumerate(lessons, start=1):
        lines.append(f"⏰ <b>{_esc(l['time'])}</b>")
        lines.append(f"📚 <b>{_esc(l['subject'])}</b>")

        details = []
        if l['classroom']:
            details.append(f"📍 {_esc(l['classroom'])}")
        if l['subgroup']:
            details.append(f"👥 {_esc(l['subgroup'])}")
        if details:
            lines.append(" · ".join(details))

        if l['teacher']:
            lines.append(f"👤 <i>{_esc(l['teacher'])}</i>")
        if idx < len(lessons):
            lines.append("")

    return "\n".join(lines)


def _week_window(today: date, offset_weeks: int) -> tuple[date, date]:
    """Понедельник–воскресенье недели, сдвинутой на offset_weeks от сегодняшней."""
    monday = today - timedelta(days=today.weekday()) + timedelta(weeks=offset_weeks)
    return monday, monday + timedelta(days=6)


def _format_week(week_days: list[dict], group_name: str, monday: date, sunday: date) -> str:
    sep = "\n" + ("=" * 16) + "\n"
    blocks = [f"<b>📅 Неделя {monday.strftime('%d.%m')} – {sunday.strftime('%d.%m')} — "
              f"{_esc(group_name)}</b>\n"]
    blocks.extend(format_day_schedule(d, group_name) + sep for d in week_days)
    return "".join(blocks)[: -len(sep)]


def get_schedule_view(group_name: str, mode: str = 'today', force_refresh: bool = False,
                      offset_weeks: int = 0) -> str:
    """Формирует текст расписания в зависимости от выбранного режима.

    mode:
    - 'today': расписание на сегодня, а если сегодня пар нет — на ближайший учебный день.
    - 'tomorrow': расписание на завтра.
    - 'week': расписание на текущую неделю (или сдвинутую на offset_weeks).
    offset_weeks != 0 в режиме 'today' показывает всю неделю со сдвигом —
    это режим кнопок «Прошлая/Следующая неделя».
    """
    offset_weeks = max(0, int(offset_weeks or 0))
    days = get_group_schedule(group_name, force_refresh=force_refresh)
    if not days:
        return (f"<b>📅 Расписание — {_esc(group_name)}</b>\n\n"
                f"Занятий в расписании не найдено.\n"
                f"Проверь название группы на <a href='{SCHEDULE_MAIN_URL}'>сайте УрГПУ</a>.")

    today = date.today()
    tomorrow = today + timedelta(days=1)

    if mode == 'week' or (mode == 'today' and offset_weeks > 0):
        monday, sunday = _week_window(today, offset_weeks)
        week_days = [d for d in days if d['date'] and monday <= d['date'] <= sunday]
        if not week_days:
            return (f"<b>📅 Неделя {monday.strftime('%d.%m')} – {sunday.strftime('%d.%m')} — "
                    f"{_esc(group_name)}</b>\n\n"
                    "<i>На эту неделю пар не найдено (каникулы или сайт не отдаёт расписание).</i>")
        return _format_week(week_days, group_name, monday, sunday)

    if mode == 'today':
        # Ищем точный день
        for d in days:
            if d['date'] == today:
                return format_day_schedule(d, group_name)

        # Если сегодня выходной или пар нет, ищем ближайший следующий день
        next_day = None
        for d in days:
            if d['date'] and d['date'] >= today:
                next_day = d
                break

        if next_day:
            note = "💡 На сегодня занятий нет. Ближайший учебный день:"
            return format_day_schedule(next_day, group_name, note=note)

        # Если все даты в прошлом — покажем первый доступный день
        return format_day_schedule(days[0], group_name, note="💡 Актуальные пары:")

    if mode == 'tomorrow':
        for d in days:
            if d['date'] == tomorrow:
                return format_day_schedule(d, group_name)

        # Если на завтра нет
        next_day = None
        for d in days:
            if d['date'] and d['date'] > tomorrow:
                next_day = d
                break

        if next_day:
            note = f"💡 На завтра ({tomorrow.strftime('%d.%m')}) занятий нет. Следующий учебный день:"
            return format_day_schedule(next_day, group_name, note=note)

        return (f"<b>📅 Расписание — {_esc(group_name)}</b>\n\n"
                f"На завтра ({tomorrow.strftime('%d.%m')}) занятий не запланировано! 🎉")

    return format_day_schedule(days[0], group_name)
