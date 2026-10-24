import html
import logging
import re
import threading
import time
from datetime import datetime

import requests

from app.bot.jokes import joke, joke_html

logger = logging.getLogger(__name__)

HELP_TEXT = (
    "<b>Что я умею</b>\n"
    "📅 Расписание — динамическое расписание пар УрГПУ (uspu.ru)\n"
    "📊 Статус — все работы и дедлайны Timeline\n"
    "📋 На сегодня — дедлайны на сегодня и завтра\n"
    "💀 Просрочки — неотправленные просроченные работы\n"
    "🔄 Проверить — зайти в Timeline прямо сейчас\n"
    "⚙️ Профиль — группа, логин, уведомления, интервал\n"
    "❓ Помощь — эта справка\n\n"
    "Кнопки — внизу экрана. Команды:\n"
    "/schedule — расписание занятий\n"
    "/group МКТ-2531 — сменить учебную группу\n"
    "/status — работы и дедлайны\n"
    "/stats — общая статистика\n"
    "/today — горящие работы (сегодня/завтра)\n"
    "/find слово — поиск работы по названию\n"
    "/feedback текст — написать администратору\n"
    "/overdue — долги\n"
    "/check — проверить Timeline\n"
    "/interval 15 — интервал проверок (мин)\n"
    "/login — сменить логин/пароль ЭИОС\n"
    "/unlink — удалить свои данные\n"
    "/cancel — отмена ввода."
)

BTN_STATUS = '📊 Статус'
BTN_OVERDUE = '💀 Просрочки'
BTN_CHECK = '🔄 Проверить'
BTN_PROFILE = '⚙️ Профиль'
BTN_HELP = '❓ Помощь'
BTN_SCHEDULE = '📅 Расписание'
BTN_TODAY = '📋 На сегодня'

# --- список работ постранично ---
STATUS_PAGE_SIZE = 4
SORT_NEW = 'new'        # новые работы (по дате появления) сверху
SORT_DEADLINE = 'dl'    # по срокам: что горит — выше

SORT_TITLES = {
    SORT_NEW: 'новые сверху, старьё в конце',
    SORT_DEADLINE: 'по срокам: сначала то, что горит',
}

# Как выглядит название учебной группы
_GROUP_RE = re.compile(r'^[A-Za-zА-Яа-яЁё0-9][A-Za-zА-Яа-яЁё0-9\-\s]{0,49}$')


def _format_countdown(deadline_str: str) -> str:
    """Форматирует обратный отсчёт до дедлайна."""
    try:
        dt = datetime.strptime(deadline_str, '%d.%m.%Y %H:%M')
    except (ValueError, TypeError):
        return ''
    delta = dt - datetime.now()
    total_sec = delta.total_seconds()
    if total_sec < 0:
        overdue_h = abs(int(total_sec // 3600))
        if overdue_h >= 24:
            return f'💀 просрочена на {overdue_h // 24} дн.'
        return f'💀 просрочена на {overdue_h} ч.'
    hours = int(total_sec // 3600)
    minutes = int((total_sec % 3600) // 60)
    days = hours // 24
    rem_h = hours % 24
    if days >= 2:
        return f'⏳ {days} дн. {rem_h} ч.'
    elif hours >= 24:
        return f'🔴 {days} дн. {rem_h} ч.'
    elif hours >= 1:
        return f'🚨 {hours} ч. {minutes} мин.'
    return f'🚨 {minutes} мин.'


def _status_icon(status: str, is_overdue: bool) -> str:
    """Эмодзи-иконка для статуса работы."""
    if is_overdue:
        return '💀'
    s = (status or '').lower()
    if any(k in s for k in ('проверена', 'зачтена', 'отлично', 'хорошо', 'удовлетворит')):
        return '✅'
    if 'отправлена' in s and 'не ' not in s:
        return '📬'
    if 'на провер' in s:
        return '🔍'
    return '⏳'


class TelegramBot:
    """Один бот на всех пользователей: long-polling + отправка уведомлений."""

    def __init__(self, token: str, app=None, api_base: str = 'https://api.telegram.org',
                 proxy: str = None, timeout: int = 20, poll_timeout: int = 25):
        self.token = token
        self.app = app
        self.api_base = (api_base or 'https://api.telegram.org').rstrip('/')
        self.proxy = proxy
        self.timeout = timeout
        self.poll_timeout = poll_timeout
        self._poll_thread = None
        self._stop_flag = threading.Event()
        self._last_update_id = 0
        self._lock = threading.Lock()
        self._last_error = None
        self._login_attempts = {}

    # ------------------------------------------------------------------ API

    def _api(self, method: str, payload: dict = None, timeout: int = None):
        if not self.token:
            return None
        url = f"{self.api_base}/bot{self.token}/{method}"
        kwargs = {
            'json': payload or {},
            'timeout': timeout or self.timeout,
        }
        if self.proxy:
            kwargs['proxies'] = {'http': self.proxy, 'https': self.proxy}
        try:
            r = requests.post(url, **kwargs)
            data = r.json()
            if not data.get('ok'):
                self._last_error = data.get('description') or str(data)
                # «message is not modified» — не ошибка: содержимое уже такое
                if 'message is not modified' in self._last_error:
                    return 'not_modified'
                logger.warning(f"Telegram API {method}: {self._last_error}")
                return None
            self._last_error = None
            return data.get('result')
        except requests.RequestException as e:
            self._last_error = str(e)
            logger.error(f"Telegram API {method} error: {e}")
            return None
        except ValueError:
            self._last_error = 'не JSON в ответе'
            logger.error(f"Telegram API {method}: не JSON в ответе")
            return None

    def get_me(self) -> dict | None:
        return self._api('getMe')

    def send_message(self, chat_id, text: str, reply_markup: dict = None) -> bool:
        return self.send_message_id(chat_id, text, reply_markup) is not None

    def send_message_id(self, chat_id, text: str, reply_markup: dict = None) -> int | None:
        """Отправка сообщения; возвращает message_id (нужен для правки на месте)."""
        if not self.token or chat_id is None:
            return None
        payload = {
            'chat_id': str(chat_id),
            'text': text[:4096],
            'parse_mode': 'HTML',
            'disable_web_page_preview': True,
        }
        if reply_markup:
            payload['reply_markup'] = reply_markup
        res = self._api('sendMessage', payload)
        if isinstance(res, dict):
            return res.get('message_id')
        return None

    def edit_message(self, chat_id, message_id: int, text: str,
                     reply_markup: dict = None) -> bool:
        if not message_id:
            return False
        payload = {
            'chat_id': str(chat_id),
            'message_id': message_id,
            'text': text[:4096],
            'parse_mode': 'HTML',
            'disable_web_page_preview': True,
        }
        if reply_markup:
            payload['reply_markup'] = reply_markup
        res = self._api('editMessageText', payload)
        return res is not None

    def delete_message(self, chat_id, message_id: int) -> bool:
        return self._api('deleteMessage',
                         {'chat_id': str(chat_id), 'message_id': message_id}) is not None

    def answer_callback(self, callback_id: str, text: str = ''):
        self._api('answerCallbackQuery', {'callback_query_id': callback_id, 'text': text})

    # --------------------------------------------------------------- клавиши

    @staticmethod
    def _main_keyboard() -> dict:
        """Постоянное меню внизу экрана (reply keyboard)."""
        return {
            'keyboard': [
                [{'text': BTN_STATUS}, {'text': BTN_OVERDUE}],
                [{'text': BTN_CHECK}, {'text': BTN_PROFILE}],
                [{'text': BTN_SCHEDULE}, {'text': BTN_HELP}],
                [{'text': BTN_TODAY}],
            ],
            'resize_keyboard': True,
            'is_persistent': True,
            'input_field_placeholder': 'Выбери пункт меню',
        }

    @staticmethod
    def _profile_keyboard(user) -> dict:
        group_lbl = user.current_group if hasattr(user, 'current_group') else 'МКТ-2531'
        return {
            'inline_keyboard': [
                [{'text': f"👥 Группа: {group_lbl}", 'callback_data': 'change_group'}],
                [{'text': '🔑 Сменить логин/пароль ЭИОС', 'callback_data': 'relogin'}],
                [{'text': ('🔕 Выключить уведомления' if user.notify_enabled
                           else '🔔 Включить уведомления'),
                  'callback_data': 'toggle_notify'}],
                [{'text': ('📅 Расписание на завтра: ВКЛ ✅' if user.schedule_notify
                           else '📅 Расписание на завтра: ВЫКЛ'),
                  'callback_data': 'toggle_schedule_notify'}],
                [{'text': ('🌅 Дайджест утром: ВКЛ ✅' if user.digest_notify
                           else '🌅 Дайджест утром: ВЫКЛ'),
                  'callback_data': 'toggle_digest_notify'}],
                [{'text': '🗑 Удалить мои данные', 'callback_data': 'unlink'}],
            ]
        }

    @staticmethod
    def _schedule_keyboard(group_name: str, offset_weeks: int = 0) -> dict:
        nav = []
        if offset_weeks > 0:
            nav.append({'text': '◀ Прошлая', 'callback_data': f'sch|today|{offset_weeks-1}'})
        nav.append({'text': '📍 Сегодня' if offset_weeks == 0 else f'📅 нед.+{offset_weeks}',
                    'callback_data': 'sch|today|0'})
        nav.append({'text': 'Следующая ▶', 'callback_data': f'sch|today|{offset_weeks+1}'})

        return {
            'inline_keyboard': [
                [{'text': '📍 Сегодня', 'callback_data': 'sch|today|0'},
                 {'text': '⏩ Завтра', 'callback_data': 'sch|tomorrow|0'}],
                nav,
                [{'text': '📆 Вся неделя', 'callback_data': f'sch|week|{offset_weeks}'},
                 {'text': '🔄 Обновить', 'callback_data': 'sch|refresh|0'}],
                [{'text': f'👥 Группа: {group_name}', 'callback_data': 'change_group'}],
            ]
        }

    @staticmethod
    def _admin_keyboard() -> dict:
        return {'inline_keyboard': [
            [{'text': '🔄 Проверить всех', 'callback_data': 'admin_check'}],
        ]}

    @staticmethod
    def _norm(text: str) -> str:
        """'📊 Статус' -> 'статус' (чтобы кнопки и команды матчились одинаково)."""
        return ''.join(ch for ch in (text or '').lower()
                       if ch.isalnum() or ch in ' /_.-').strip()

    # ------------------------------------------------------------ утилиты БД

    def _get_user(self, chat_id: str):
        from app.models import User
        return User.query.filter_by(chat_id=str(chat_id)).first()

    def _get_or_create_user(self, chat: dict, from_user: dict):
        from app.models import db, User
        chat_id = str(chat.get('id'))
        user = self._get_user(chat_id)
        if user is None:
            user = User(chat_id=chat_id, state='login', group_name='МКТ-2531')
            db.session.add(user)
        user.username = from_user.get('username') or user.username
        user.first_name = from_user.get('first_name') or user.first_name
        db.session.commit()
        return user

    # -------------------------------------------------------------- команды

    def _cmd_start(self, user, chat_id: str):
        if user.is_registered and user.password_enc:
            self.send_message(
                chat_id,
                f"<b>Привет, {html.escape(user.first_name or 'студент')}!</b>\n"
                f"Слежу за твоим Timeline (<code>{html.escape(user.timeline_login or '')}</code>).\n"
                f"Твоя группа для расписания: <b>{html.escape(user.current_group)}</b>.\n\n"
                "Кнопки — внизу экрана, подробности: ❓ Помощь\n\n"
                + joke_html('start'),
                reply_markup=self._main_keyboard(),
            )
        else:
            self._ask_login(user, chat_id)

    def _ask_login(self, user, chat_id: str, intro: bool = True):
        from app.models import db
        user.state = 'login'
        user.pending_login = None
        db.session.commit()
        head = ("<b>Регистрация в Timeline</b>\n" if intro else "<b>Смена данных ЭИОС</b>\n")
        self.send_message(
            chat_id,
            head +
            "Отправь свой <b>логин ЭИОС</b> (как на eios.rsvpu.ru, например "
            "<code>ivanov.i.i</code>).\n"
            "<i>(Расписание доступно и без логина через кнопку «📅 Расписание» или /schedule)</i>\n"
            "Отмена: /cancel\n\n"
            + joke_html('login'),
        )

    def _ask_password(self, user, chat_id: str):
        from app.models import db
        user.state = 'password'
        db.session.commit()
        self.send_message(
            chat_id,
            f"Логин: <code>{html.escape(user.pending_login or '')}</code>\n"
            "Теперь отправь <b>пароль</b>. Сообщение с паролем я сразу удалю "
            "из чата, а в базе он хранится зашифрованным.\n"
            "Отмена: /cancel\n\n"
            + joke_html('password'),
        )

    def _finish_registration(self, user_id: int, login: str, password: str, chat_id: str,
                             status_message_id: int = None):
        """Проверяем логин/пароль в Timeline и сохраняем (в отдельном потоке)."""
        import json
        from app.models import db, User
        from app.parser.auth import (REASON_NETWORK, REASON_TEXT, try_login)
        from app.scheduler.jobs import check_user
        from app.security import encrypt

        def reply(text: str, markup: dict = None):
            if status_message_id and self.edit_message(chat_id, status_message_id, text,
                                                       markup):
                return
            self.send_message(chat_id, text, reply_markup=markup)

        with self.app.app_context():
            user = db.session.get(User, user_id)
            if user is None:
                return
            res = try_login(self.app, login, password)
            ok = res[0]
            reason = res[1] if len(res) > 1 else None
            cookies = res[2] if len(res) > 2 else None

            if not ok:
                if reason == REASON_NETWORK:
                    user.state = 'login'
                    user.pending_login = None
                    db.session.commit()
                    reply("⛔ Timeline сейчас не отвечает (обрыв связи), поэтому проверить "
                          "данные не смог.\nЭто не значит, что пароль неверный — попробуй "
                          "ещё раз позже: /login\n\n" + joke_html('error'))
                    return
                user.state = 'login'
                user.pending_login = None
                db.session.commit()
                reply("⛔ Timeline не принял эти логин/пароль"
                      + (f" ({html.escape(REASON_TEXT.get(reason, reason))})" if reason else '')
                      + ".\nПроверь данные и отправь логин заново (или /cancel).\n\n"
                      + joke_html('login'))
                return

            user.timeline_login = login
            user.password_enc = encrypt(password)
            user.is_registered = True
            user.is_active = True
            user.state = 'ready'
            user.pending_login = None
            user.last_error = None
            if cookies:
                user.session_cookies = encrypt(json.dumps(cookies))
            db.session.commit()

        check_user(self.app, user_id, notify=False, force=True)

        with self.app.app_context():
            from app.models import db, User
            user = db.session.get(User, user_id)
            if user is None:
                return
            text = ("✅ <b>Готово, вход в Timeline работает.</b>\n"
                    + joke_html('registered') + "\n\n"
                    + self._build_status_text(user))
            reply(text, self._main_keyboard())

    def _cmd_unlink(self, user, chat_id: str):
        from app.models import db, WorkItem
        WorkItem.query.filter_by(user_id=user.id).delete()
        user.timeline_login = None
        user.password_enc = None
        user.session_cookies = None
        user.is_registered = False
        user.state = 'login'
        user.pending_login = None
        user.last_error = None
        db.session.commit()
        self.send_message(chat_id, "🗑 Данные удалены. Чтобы начать заново: /login\n\n"
                          + joke_html('unlink'))

    def _menu(self, chat_id: str):
        self.send_message(chat_id, "Меню — кнопки внизу 👇\n\n" + joke_html('help'),
                          reply_markup=self._main_keyboard())

    def _help_text(self) -> str:
        return HELP_TEXT + "\n\n" + joke_html('help')

    # ------------------------------------------------------------ расписание

    def _cmd_schedule(self, user, chat_id: str, mode: str = 'today',
                      force_refresh: bool = False, message_id: int = None,
                      offset_weeks: int = 0):
        from app.parser.schedule import get_schedule_view
        group = getattr(user, 'current_group', 'МКТ-2531')
        try:
            text = get_schedule_view(group, mode=mode, force_refresh=force_refresh,
                                     offset_weeks=offset_weeks)
        except Exception as e:
            logger.error(f"Ошибка загрузки расписания: {e}")
            text = (f"⚠️ Не удалось загрузить расписание для группы <b>{html.escape(group)}</b>.\n"
                    f"Причина: <code>{html.escape(str(e))}</code>")

        keyboard = self._schedule_keyboard(group, offset_weeks)
        if message_id:
            if not self.edit_message(chat_id, message_id, text, keyboard):
                self.send_message(chat_id, text, reply_markup=keyboard)
        else:
            self.send_message(chat_id, text, reply_markup=keyboard)

    def _cmd_set_group(self, user, chat_id: str, group_name: str):
        from app.models import db
        from app.parser.schedule import get_available_groups
        raw = group_name.strip()
        if not raw:
            self.send_message(chat_id, "Использование: <code>/group МКТ-2531</code>")
            return
        if not _GROUP_RE.fullmatch(raw):
            self.send_message(
                chat_id,
                "⚠️ Название группы — 1–50 символов: буквы, цифры, дефис и пробелы.\n"
                "Например: <code>/group МКТ-2531</code>")
            return

        # сверяемся со списком групп на сайте (кэш 24 ч); сайт недоступен —
        # принимаем как есть, чтобы не блокировать человека из-за таймаута
        canonical = raw.upper()
        groups = []
        try:
            groups = get_available_groups()
        except Exception as e:
            logger.warning(f"Не удалось получить список групп: {e}")
        if groups:
            upper_map = {g.upper(): g for g in groups}
            if canonical not in upper_map:
                guesses = [g for g in groups if raw.lower() in g.lower()][:5]
                suggest = ("\nПохожие: " + ", ".join(f"<code>{html.escape(g)}</code>"
                                                     for g in guesses)) if guesses else ""
                self.send_message(
                    chat_id,
                    f"⚠️ Группы <b>{html.escape(raw)}</b> нет в расписании УрГПУ."
                    f"{suggest}\nПроверь написание или пришли /group ещё раз.")
                return
            canonical = upper_map[canonical].upper()

        user.group_name = canonical
        user.state = 'ready' if user.is_registered else 'login'
        db.session.commit()
        self.send_message(
            chat_id,
            f"✅ Твоя академическая группа установлена: <b>{html.escape(canonical)}</b>.",
            reply_markup=self._main_keyboard()
        )
        self._cmd_schedule(user, chat_id, mode='today')

    # ------------------------------------------------------------ текст меню

    @staticmethod
    def _silly_bar(page: int, pages: int) -> str:
        """Прогресс-бар страницы: ▰▰▱▱▱▱▱▱▱▱"""
        filled = max(1, round(page / max(pages, 1) * 10))
        return '▰' * filled + '▱' * (10 - filled)

    @staticmethod
    def _deadline_key(item):
        """Для сортировки по срокам: строку 'ДД.ММ.ГГГГ ЧЧ:ММ' в datetime."""
        try:
            return datetime.strptime(item.deadline or '', '%d.%m.%Y %H:%M')
        except ValueError:
            return datetime.max

    def _status_items(self, user, sort: str) -> list:
        from app.models import WorkItem

        items = WorkItem.query.filter_by(user_id=user.id).all()
        if sort == SORT_DEADLINE:
            items.sort(key=self._deadline_key)
        else:
            items.sort(key=lambda it: (it.first_seen_at or datetime.min, it.id),
                       reverse=True)
        return items

    def _status_keyboard(self, page: int, pages: int, sort: str) -> dict:
        nav = []
        if page > 1:
            nav.append({'text': '⬅️ Назад', 'callback_data': f'st|{sort}|{page - 1}'})
        nav.append({'text': f'стр. {page}/{pages}', 'callback_data': 'noop'})
        if page < pages:
            nav.append({'text': 'Вперёд ➡️', 'callback_data': f'st|{sort}|{page + 1}'})

        other = SORT_DEADLINE if sort == SORT_NEW else SORT_NEW
        toggle = ('⏰ По срокам' if sort == SORT_NEW else '🆕 Новые сверху')
        return {'inline_keyboard': [
            nav,
            [
                {'text': toggle, 'callback_data': f'st|{other}|1'},
                {'text': '🔽 Компактно', 'callback_data': 'st_compact'},
            ],
        ]}

    def _build_status_page(self, user, page: int = 1,
                           sort: str = SORT_NEW) -> tuple[str, dict | None]:
        """Страница списка работ: текст + клавиатура листалки."""
        from app.services import enrich_work_item as _enrich

        items = self._status_items(user, sort)
        if not items:
            return ("<i>Работ пока нет. Нажми «🔄 Проверить».</i>\n"
                    "Если это повторяется — проверь логин/пароль: /login\n\n"
                    + joke_html('empty')), None

        pages = max(1, (len(items) + STATUS_PAGE_SIZE - 1) // STATUS_PAGE_SIZE)
        page = min(max(1, page), pages)
        start = (page - 1) * STATUS_PAGE_SIZE
        chunk = items[start:start + STATUS_PAGE_SIZE]

        lines = [f"<b>📊 Работы — стр. {page}/{pages}</b>  <i>(всего {len(items)}, "
                 f"{SORT_TITLES.get(sort, '')})</i>",
                 f"<code>{self._silly_bar(page, pages)}</code>", ""]

        for offset, it in enumerate(chunk):
            e = _enrich(it)
            icon = _status_icon(e['status'], e['is_overdue'])
            lines.append(f"{icon} <b>{start + offset + 1}. {html.escape(e['discipline'])}</b>")
            if e['deadline']:
                countdown = _format_countdown(e['deadline'])
                lines.append(f"   Срок: {e['deadline']}" + (f'  {countdown}' if countdown else ''))
            lines.append(f"   Статус: {e['status']}")
            lines.append("")

        if user.last_error:
            lines.append("⚠️ Последняя проверка не удалась: "
                         f"<code>{html.escape(user.last_error)}</code>")

        lines.append(joke_html('status_last' if page == pages else 'status', page - 1))

        return "\n".join(lines), self._status_keyboard(page, pages, sort)

    def _build_status_compact(self, user) -> tuple[str, dict]:
        """Компактная сводка по работам (без подробного списка)."""
        from app.models import WorkItem
        from app.services import enrich_work_item as _enrich

        items = WorkItem.query.filter_by(user_id=user.id).all()
        if not items:
            return ("<i>Работ пока нет. Нажми «🔄 Проверить».</i>", None)

        enriched = [_enrich(it) for it in items]
        total = len(enriched)
        overdue_cnt = sum(1 for e in enriched if e['is_overdue'])
        sent_cnt = sum(1 for e in enriched if e['status'] not in ('', 'Не отправлена'))
        pending_cnt = total - sent_cnt - overdue_cnt

        lines = [
            "<b>📊 Сводка работ (компактный вид)</b>",
            "",
            f"Всего: <b>{total}</b>",
            f"✅ Сдано: <b>{sent_cnt}</b>",
            f"⏳ В процессе: <b>{max(0, pending_cnt)}</b>",
            f"💀 Просрочено: <b>{overdue_cnt}</b>",
            "",
            joke_html('status', 0)
        ]
        keyboard = {
            'inline_keyboard': [
                [{'text': '📄 Подробный список', 'callback_data': 'st|new|1'}],
            ]
        }
        return "\n".join(lines), keyboard

    def _build_status_text(self, user) -> str:
        return self._build_status_page(user)[0]

    def _build_overdue_text(self, user) -> str:
        from app.models import WorkItem
        from app.services import enrich_work_item as _enrich

        overdue = [_enrich(it) for it in WorkItem.query.filter_by(user_id=user.id).all()]
        overdue = [e for e in overdue if e['is_overdue']]
        if not overdue:
            return ("<i>Просроченных неотправленных работ нет. Отличная работа! 👍</i>\n\n"
                    + joke_html('overdue_clean'))

        lines = [f"<b>💀 Просроченные работы ({len(overdue)}):</b>\n"]
        for e in overdue:
            lines.append(f"• <b>{html.escape(e['discipline'])}</b>")
            lines.append(f"  Срок: {e['deadline']}")
            lines.append("")
        lines.append(joke_html('overdue'))
        return "\n".join(lines)

    def _build_last_check_text(self, user) -> str:
        if not user.last_check_at:
            return "<i>Проверка ещё не запускалась.</i>\n\n" + joke_html('last_check')
        ts = user.last_check_at.strftime('%d.%m.%Y %H:%M:%S')
        interval = user.check_interval or self.app.config.get('DEFAULT_CHECK_INTERVAL', 300)
        text = (f"<b>Последняя проверка:</b>\n{ts} UTC\n\n"
                f"Интервал: {interval // 60} мин\n")
        if user.last_error:
            text += f"\n⚠️ Ошибка: <code>{html.escape(user.last_error)}</code>\n"
        return text + "\n" + joke_html('last_check')

    def _build_profile_text(self, user) -> str:
        interval = user.check_interval or self.app.config.get('DEFAULT_CHECK_INTERVAL', 300)
        group = getattr(user, 'current_group', 'МКТ-2531')
        schedule_notify_str = 'ВКЛ ✅' if getattr(user, 'schedule_notify', False) else 'ВЫКЛ'
        digest_notify_str = 'ВКЛ ✅' if getattr(user, 'digest_notify', False) else 'ВЫКЛ'
        return (
            "<b>⚙️ Профиль</b>\n"
            f"Академ. группа: <code>{html.escape(group)}</code> (/group)\n"
            f"Логин ЭИОС: <code>{html.escape(user.timeline_login or '—')}</code>\n"
            f"Пароль: {'зашифрован и сохранён' if user.password_enc else '—'}\n"
            f"Сессия: {'активна' if user.session_cookies else 'не создана'}\n"
            f"Уведомления: {'включены' if user.notify_enabled else 'выключены'}\n"
            f"Расписание на завтра: {schedule_notify_str}\n"
            f"Дайджест утром: {digest_notify_str}\n"
            f"Интервал проверки: {interval // 60} мин\n"
            f"Последняя проверка: "
            f"{user.last_check_at.strftime('%d.%m.%Y %H:%M') if user.last_check_at else '—'}\n"
            + (f"⚠️ Ошибка: <code>{html.escape(user.last_error)}</code>\n"
               if user.last_error else "")
            + "\n" + joke_html('profile')
        )

    # --------------------------------------------------------------- callback

    def _handle_callback(self, cb: dict):
        from app.models import db

        cb_id = cb.get('id')
        data = cb.get('data', '')
        msg = cb.get('message') or {}
        chat = msg.get('chat') or {}
        chat_id = str(chat.get('id', ''))
        message_id = msg.get('message_id')
        from_user = cb.get('from') or {}

        user = self._get_or_create_user(chat, from_user)

        if data == 'menu':
            self.answer_callback(cb_id)
            self._menu(chat_id)
            return

        if data == 'noop':
            self.answer_callback(cb_id)
            return

        # Действия с расписанием доступны всегда!
        if data.startswith('sch|'):
            self.answer_callback(cb_id)
            parts = data.split('|')
            mode = parts[1] if len(parts) > 1 else 'today'
            offset_weeks = 0
            if len(parts) > 2:
                try:
                    offset_weeks = int(parts[2])
                except ValueError:
                    offset_weeks = 0
            force_refresh = (mode == 'refresh')
            if force_refresh:
                mode = 'today'
            self._cmd_schedule(user, chat_id, mode=mode, force_refresh=force_refresh,
                               message_id=message_id, offset_weeks=offset_weeks)
            return

        if data == 'change_group':
            self.answer_callback(cb_id)
            user.state = 'waiting_group'
            db.session.commit()
            self.send_message(
                chat_id,
                f"Текущая группа: <b>{html.escape(user.current_group)}</b>.\n"
                "Отправь новое название группы сообщением (например, <code>МКТ-2531</code>) или /cancel:"
            )
            return

        if not user.is_registered:
            self.answer_callback(cb_id, 'Сначала /login')
            self._ask_login(user, chat_id)
            return

        if data == 'status' or data.startswith('st|'):
            self.answer_callback(cb_id)
            sort, page = SORT_NEW, 1
            if data.startswith('st|'):
                parts = data.split('|')
                if len(parts) > 1 and parts[1] in (SORT_NEW, SORT_DEADLINE):
                    sort = parts[1]
                if len(parts) > 2 and parts[2].isdigit():
                    page = int(parts[2])
            text, keyboard = self._build_status_page(user, page, sort)
            self.edit_message(chat_id, message_id, text, keyboard)
        elif data == 'st_compact':
            self.answer_callback(cb_id)
            text, keyboard = self._build_status_compact(user)
            self.edit_message(chat_id, message_id, text, keyboard)
        elif data == 'overdue':
            self.answer_callback(cb_id)
            self.edit_message(chat_id, message_id, self._build_overdue_text(user))
        elif data == 'last':
            self.answer_callback(cb_id)
            self.edit_message(chat_id, message_id, self._build_last_check_text(user))
        elif data == 'profile':
            self.answer_callback(cb_id)
            self.edit_message(chat_id, message_id, self._build_profile_text(user),
                              self._profile_keyboard(user))
        elif data == 'toggle_notify':
            user.notify_enabled = not user.notify_enabled
            db.session.commit()
            self.answer_callback(cb_id, 'Уведомления включены' if user.notify_enabled
                                 else 'Уведомления выключены')
            self.edit_message(chat_id, message_id, self._build_profile_text(user),
                              self._profile_keyboard(user))
        elif data == 'toggle_schedule_notify':
            user.schedule_notify = not user.schedule_notify
            db.session.commit()
            self.answer_callback(cb_id, 'Расписание на завтра: ВКЛ' if user.schedule_notify
                                 else 'Расписание на завтра: ВЫКЛ')
            self.edit_message(chat_id, message_id, self._build_profile_text(user),
                              self._profile_keyboard(user))
        elif data == 'toggle_digest_notify':
            user.digest_notify = not user.digest_notify
            db.session.commit()
            self.answer_callback(cb_id, 'Дайджест утром: ВКЛ' if user.digest_notify
                                 else 'Дайджест утром: ВЫКЛ')
            self.edit_message(chat_id, message_id, self._build_profile_text(user),
                              self._profile_keyboard(user))
        elif data == 'relogin':
            self.answer_callback(cb_id)
            self._ask_login(user, chat_id, intro=False)
        elif data == 'unlink':
            self.answer_callback(cb_id, 'Данные удалены')
            self._cmd_unlink(user, chat_id)
        elif data == 'check':
            self.answer_callback(cb_id, 'Проверяю…')
            self._run_check_async(user.id, chat_id)
        elif data == 'admin':
            self.answer_callback(cb_id)
            self.edit_message(chat_id, message_id, self._admin_text(),
                              self._admin_keyboard())
        elif data == 'admin_check':
            self.answer_callback(cb_id, 'Запускаю проверку всех…')
            threading.Thread(target=self._admin_check_all, daemon=True).start()
        else:
            self.answer_callback(cb_id, 'Неизвестное действие')

    # ---------------------------------------------------------------- сообщения

    def _handle_message(self, msg: dict):
        from app.models import db

        chat = msg.get('chat') or {}
        if chat.get('type') != 'private':
            return  # групповые чаты не обслуживаем
        chat_id = str(chat.get('id'))
        text = (msg.get('text') or '').strip()
        from_user = msg.get('from') or {}
        user = self._get_or_create_user(chat, from_user)
        low = text.lower()
        norm = self._norm(text)

        is_admin = bool(self.app.config.get('ADMIN_CHAT_ID')) and \
            chat_id == str(self.app.config['ADMIN_CHAT_ID'])

        # --- команды, доступные всегда ---
        if low.startswith('/start') or norm == 'start':
            self._cmd_start(user, chat_id)
            return
        if low in ('/cancel', 'отмена') or norm == 'отмена':
            user.state = 'ready' if user.is_registered else 'login'
            user.pending_login = None
            db.session.commit()
            self.send_message(chat_id, "Отменено.")
            return
        if low in ('/help', 'help', 'помощь') or norm == 'помощь':
            self.send_message(chat_id, self._help_text(), reply_markup=self._main_keyboard()
                              if user.is_registered else None)
            return
        if low.startswith('/admin') and is_admin:
            self.send_message(chat_id, self._admin_text(),
                              reply_markup=self._admin_keyboard())
            return
        if low.startswith('/broadcast') and is_admin:
            body = text[len('/broadcast'):].strip()
            if not body:
                self.send_message(chat_id, "Использование: <code>/broadcast текст</code>")
                return
            threading.Thread(target=self._broadcast, args=(body,), daemon=True).start()
            self.send_message(chat_id, "Рассылаю…")
            return

        # --- Расписание доступно для любого пользователя ---
        if low.startswith(('/schedule', '/rasp', '/расписание')) or norm in ('расписание', 'пары'):
            self._cmd_schedule(user, chat_id, mode='today')
            return
        if low.startswith(('/group', '/группа')):
            parts = text.split(maxsplit=1)
            if len(parts) > 1:
                self._cmd_set_group(user, chat_id, parts[1])
            else:
                self.send_message(chat_id, f"Твоя группа: <b>{html.escape(user.current_group)}</b>.\n"
                                            "Чтобы изменить, напиши: <code>/group МКТ-2531</code>")
            return

        # Ввод названия группы
        if user.state == 'waiting_group':
            if text.startswith('/'):
                user.state = 'ready' if user.is_registered else 'login'
                db.session.commit()
            else:
                self._cmd_set_group(user, chat_id, text)
                return

        # --- онбординг EIOS Timeline ---
        if user.state == 'login':
            if not text or text.startswith('/'):
                self.send_message(chat_id, "Отправь логин текстом или /cancel")
                return
            user.pending_login = text
            self._ask_password(user, chat_id)
            return

        if user.state == 'password':
            self.delete_message(chat_id, msg.get('message_id'))
            password = text
            login = user.pending_login
            if not login or not password:
                self._ask_login(user, chat_id, intro=False)
                return
            user.state = 'validating'
            db.session.commit()
            status_id = self.send_message_id(chat_id, "🔎 Проверяю вход в Timeline…")
            threading.Thread(
                target=self._finish_registration,
                args=(user.id, login, password, chat_id, status_id),
                daemon=True,
            ).start()
            return

        if user.state == 'validating':
            self.send_message(chat_id, "Ещё проверяю предыдущие данные, подожди пару секунд…")
            return

        if low.startswith('/login') or norm == 'логин':
            now_ts = time.time()
            last_ts = self._login_attempts.get(chat_id, 0.0)
            if now_ts - last_ts < 5.0:
                self.send_message(chat_id, "⏳ Пожалуйста, подожди пару секунд перед повторным входом.")
                return
            if len(self._login_attempts) > 500:
                self._login_attempts = {k: v for k, v in self._login_attempts.items()
                                        if now_ts - v < 60}
            self._login_attempts[chat_id] = now_ts
            self._ask_login(user, chat_id, intro=False)
            return
        if low.startswith('/unlink') or low.startswith('/logout'):
            self._cmd_unlink(user, chat_id)
            return

        if not user.is_registered:
            self._ask_login(user, chat_id)
            return

        # --- меню и действия зарегистрированного пользователя ---
        if low.startswith('/menu') or norm == 'меню':
            self._menu(chat_id)
        elif low.startswith('/status') or norm == 'статус':
            text, keyboard = self._build_status_page(user)
            self.send_message(chat_id, text, reply_markup=keyboard)
        elif low.startswith('/stats') or norm == 'статистика':
            self._cmd_stats(user, chat_id)
        elif low.startswith('/today') or norm == 'на сегодня' or text == BTN_TODAY:
            self._cmd_today(user, chat_id)
        elif low.startswith('/find') or norm.startswith('поиск'):
            parts = text.split(maxsplit=1)
            query = parts[1].strip() if len(parts) > 1 else ''
            self._cmd_find(user, chat_id, query)
        elif low.startswith('/feedback') or norm.startswith('отзыв'):
            parts = text.split(maxsplit=1)
            msg_text = parts[1].strip() if len(parts) > 1 else ''
            self._cmd_feedback(user, chat_id, msg_text)
        elif low.startswith('/overdue') or norm in ('просрочки', 'просроченные'):
            self.send_message(chat_id, self._build_overdue_text(user))
        elif low.startswith('/check') or norm == 'проверить':
            self._run_check_async(user.id, chat_id)
        elif low.startswith('/profile') or norm == 'профиль':
            self.send_message(chat_id, self._build_profile_text(user),
                              reply_markup=self._profile_keyboard(user))
        elif low.startswith('/last') or norm == 'последняя проверка':
            self.send_message(chat_id, self._build_last_check_text(user))
        elif low.startswith('/interval'):
            parts = text.split()
            if len(parts) > 1 and parts[1].isdigit() and int(parts[1]) >= 5:
                user.check_interval = int(parts[1]) * 60
                db.session.commit()
                self.send_message(chat_id, f"Интервал проверки: {parts[1]} мин")
            else:
                self.send_message(chat_id,
                                  "Использование: <code>/interval 15</code> (минуты, минимум 5)")
        else:
            self.send_message(chat_id, "Не понял команду. Жми кнопки внизу или /help\n\n"
                              + joke_html('unknown'),
                              reply_markup=self._main_keyboard())

    # ----------------------------------------------------- проверки (в потоках)

    def _cmd_stats(self, user, chat_id: str):
        """Статистика работ пользователя."""
        from app.models import WorkItem
        from app.services import enrich_work_item as _enrich

        items = WorkItem.query.filter_by(user_id=user.id).all()
        if not items:
            self.send_message(chat_id, '<i>Работ пока нет. Нажми «🔄 Проверить».</i>')
            return

        enriched = [_enrich(it) for it in items]
        total = len(enriched)
        overdue_list = [e for e in enriched if e['is_overdue']]
        sent = [e for e in enriched if e['status'] not in ('', 'Не отправлена')]
        pending = [e for e in enriched if e['status'] in ('', 'Не отправлена') and not e['is_overdue']]

        # горит = pending у которых deadline <= 48ч
        now = datetime.now()
        burning = []
        nearest = None
        nearest_dt = None
        for it in items:
            if (it.status or '') not in ('', 'Не отправлена'):
                continue
            if not it.deadline:
                continue
            try:
                dt = datetime.strptime(it.deadline, '%d.%m.%Y %H:%M')
            except ValueError:
                continue
            delta = (dt - now).total_seconds()
            if 0 < delta <= 48 * 3600:
                burning.append(it)
            if delta > 0 and (nearest_dt is None or dt < nearest_dt):
                nearest_dt = dt
                nearest = it

        lines = [
            '<b>📊 Статистика работ</b>',
            '',
            f'Всего работ: <b>{total}</b>',
            f'✅ Сдано/проверено: <b>{len(sent)}</b>',
            f'⏳ Ожидает отправки: <b>{len(pending)}</b>',
            f'💀 Просрочено: <b>{len(overdue_list)}</b>',
            f'🔴 Горит (≤48 ч.): <b>{len(burning)}</b>',
        ]

        if nearest:
            countdown = _format_countdown(nearest.deadline)
            lines += [
                '',
                '⏰ Ближайший дедлайн:',
                f'<b>{html.escape(nearest.discipline)}</b>',
                f'Срок: {nearest.deadline}  {countdown}',
            ]

        lines += ['', f'👥 Группа: <code>{html.escape(user.current_group)}</code>']
        self.send_message(chat_id, '\n'.join(lines))

    def _cmd_today(self, user, chat_id: str):
        """Дедлайны на сегодня и завтра."""
        from app.models import WorkItem
        from datetime import timedelta

        now = datetime.now()
        tomorrow = now + timedelta(days=1)
        items = WorkItem.query.filter_by(user_id=user.id).all()

        urgent = []
        for it in items:
            if (it.status or '') not in ('', 'Не отправлена'):
                continue
            if not it.deadline:
                continue
            try:
                dt = datetime.strptime(it.deadline, '%d.%m.%Y %H:%M')
            except ValueError:
                continue
            if dt.date() <= tomorrow.date():
                urgent.append((dt, it))

        urgent.sort(key=lambda x: x[0])

        if not urgent:
            self.send_message(chat_id, '✅ <b>Горящих дедлайнов нет!</b>\n\n'
                              '<i>Работ на сегодня и завтра нет — можно выдохнуть.</i>\n\n'
                              + joke_html('overdue_clean'))
            return

        lines = [f'<b>📋 Дедлайны сегодня и завтра ({len(urgent)}):</b>', '']
        for dt, it in urgent:
            countdown = _format_countdown(it.deadline)
            overdue = dt < now
            icon = '💀' if overdue else ('🚨' if (dt - now).total_seconds() < 12 * 3600 else '🔴')
            lines.append(f'{icon} <b>{html.escape(it.discipline)}</b>')
            lines.append(f'   Срок: {it.deadline}  {countdown}')
            lines.append('')

        self.send_message(chat_id, '\n'.join(lines))

    def _cmd_find(self, user, chat_id: str, query: str):
        """Поиск работы по названию дисциплины."""
        from app.models import WorkItem
        from app.services import enrich_work_item as _enrich

        if not query:
            self.send_message(chat_id, "ℹ️ Напиши поисковый запрос, например:\n<code>/find история</code> или <code>/find матем</code>")
            return

        q_lower = query.lower()
        items = WorkItem.query.filter_by(user_id=user.id).all()
        matched = [it for it in items if q_lower in (it.discipline or '').lower()]

        if not matched:
            self.send_message(chat_id, f"🔍 По запросу «<b>{html.escape(query)}</b>» ничего не найдено.")
            return

        lines = [f"🔍 <b>Результаты поиска ({len(matched)}):</b>\n"]
        for idx, it in enumerate(matched, start=1):
            e = _enrich(it)
            icon = _status_icon(e['status'], e['is_overdue'])
            countdown = _format_countdown(e['deadline']) if e.get('deadline') else ''
            lines.append(f"{icon} <b>{idx}. {html.escape(e['discipline'])}</b>")
            if e['deadline']:
                lines.append(f"   Срок: {e['deadline']}" + (f"  {countdown}" if countdown else ""))
            lines.append(f"   Статус: {e['status']}")
            lines.append("")

        self.send_message(chat_id, "\n".join(lines))

    def _cmd_feedback(self, user, chat_id: str, msg_text: str):
        """Отправка отзыва / сообщения администратору бота."""
        if not msg_text:
            self.send_message(chat_id, "ℹ️ Отправь текст отзыва или пожелания, например:\n<code>/feedback Добавьте функцию напоминания</code>")
            return

        admin_chat_id = self.app.config.get('ADMIN_CHAT_ID')
        if not admin_chat_id:
            self.send_message(chat_id, "⚠️ Администратор не настроил обратную связь (ADMIN_CHAT_ID не задан).")
            return

        sender_info = f"@{user.username}" if user.username else (user.first_name or f"id{chat_id}")
        admin_msg = (
            "💬 <b>Новый отзыв / обратная связь</b>\n"
            f"От: <b>{html.escape(sender_info)}</b> (ID: <code>{chat_id}</code>, Группа: <code>{html.escape(user.current_group)}</code>)\n\n"
            f"{html.escape(msg_text)}"
        )
        ok = self.send_message(admin_chat_id, admin_msg)
        if ok:
            self.send_message(chat_id, "✅ Твоё сообщение успешно отправлено администратору! Спасибо.")
        else:
            self.send_message(chat_id, "⚠️ Не удалось доставить сообщение администратору.")

    def _run_check_async(self, user_id: int, chat_id: str):
        """Ручная проверка: одно сообщение, которое потом правится результатом."""
        status_id = self.send_message_id(chat_id, "🔄 Захожу в Timeline…\n\n"
                                          + joke_html('checking'))

        def worker():
            from app.scheduler.jobs import CHECK_SKIPPED, check_user
            result, error = None, None
            try:
                result = check_user(self.app, user_id, notify=False)
            except Exception as e:
                logger.error(f"Ручная проверка не удалась: {e}")
                error = str(e)
            with self.app.app_context():
                from app.models import db, User
                user = db.session.get(User, user_id)
                if user is None:
                    return
                if error:
                    text = (f"⚠️ Ошибка проверки: <code>{html.escape(error)}</code>\n\n"
                            + joke_html('error'))
                    keyboard = None
                elif result == CHECK_SKIPPED:
                    text, keyboard = self._build_status_page(user)
                    text = "🕒 Только что проверял — не долблю сайт зря.\n\n" + text
                else:
                    text, keyboard = self._build_status_page(user)
                if not self.edit_message(chat_id, status_id, text, keyboard):
                    self.send_message(chat_id, text, reply_markup=keyboard)

        threading.Thread(target=worker, daemon=True).start()

    def _admin_text(self) -> str:
        from app.models import User, WorkItem
        total = User.query.count()
        registered = User.query.filter_by(is_registered=True).count()
        active = User.query.filter_by(is_registered=True, is_active=True).count()
        works = WorkItem.query.count()
        return (
            "<b>🛠 Админ-сводка</b>\n"
            f"Всего чатов: {total}\n"
            f"С данными ЭИОС: {registered}\n"
            f"Активных: {active}\n"
            f"Работ в базе: {works}\n\n"
            + joke_html('admin')
        )

    def _admin_check_all(self):
        from app.scheduler.jobs import check_all_users
        try:
            check_all_users(self.app, force=True)
        except Exception as e:
            logger.error(f"Проверка всех не удалась: {e}")

    def _broadcast(self, body: str):
        from app.models import User
        with self.app.app_context():
            chat_ids = [u.chat_id for u in
                        User.query.filter_by(is_registered=True, is_active=True).all()]
        sent = 0
        for cid in chat_ids:
            if self.send_message(cid, body):
                sent += 1
                time.sleep(0.05)
        admin = self.app.config.get('ADMIN_CHAT_ID')
        if admin:
            self.send_message(admin, f"Рассылка: доставлено {sent} из {len(chat_ids)}")

    # -------------------------------------------------------------- polling

    def _poll_loop(self):
        logger.info("Telegram long-polling запущен")
        while not self._stop_flag.is_set():
            try:
                updates = self._api(
                    'getUpdates',
                    {'offset': self._last_update_id + 1,
                     'timeout': self.poll_timeout,
                     'allowed_updates': ['message', 'callback_query']},
                    timeout=self.poll_timeout + 10,
                )
                if updates is None:
                    # ошибка (битый токен, сеть) — не молотим API в тугом цикле
                    time.sleep(3)
                    continue
                if not updates or updates == 'not_modified':
                    continue
                for upd in updates:
                    self._last_update_id = max(self._last_update_id, upd['update_id'])
                    if self.app is None:
                        continue
                    with self.app.app_context():
                        try:
                            if 'callback_query' in upd:
                                self._handle_callback(upd['callback_query'])
                            elif 'message' in upd:
                                self._handle_message(upd['message'])
                        except Exception as e:
                            logger.error(f"Ошибка обработки update: {e}", exc_info=True)
            except Exception as e:
                logger.error(f"Polling loop error: {e}")
                time.sleep(3)
        logger.info("Telegram long-polling остановлен")

    def start_polling(self, app=None):
        if app is not None:
            self.app = app
        if not self.token:
            logger.warning("Polling не запущен: нет токена")
            return False
        if self._poll_thread and self._poll_thread.is_alive():
            return True
        self._stop_flag.clear()
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()
        return True

    def stop_polling(self):
        self._stop_flag.set()


# ----------------------------------------------------------- менеджер бота

_bot = None
_senders = {}
_bot_lock = threading.Lock()


def get_sender(app):
    """Бот для отправки сообщений: работающий поллер или временный отправитель."""
    global _bot
    if _bot is not None and _bot.token:
        return _bot
    with app.app_context():
        from app.models import Setting
        setting = Setting.query.first()
        token = (setting.telegram_bot_token or '').strip() if setting else ''
    if not token:
        return None
    with _bot_lock:
        sender = _senders.get(token)
        if sender is None:
            sender = TelegramBot(
                token, app,
                api_base=app.config['TELEGRAM_API_BASE'],
                proxy=app.config['TELEGRAM_PROXY'],
                timeout=app.config['TELEGRAM_TIMEOUT'],
                poll_timeout=app.config['TELEGRAM_POLL_TIMEOUT'],
            )
            _senders[token] = sender
    return sender


def start_bot(app):
    """Запустить long-polling с токеном из настроек."""
    global _bot
    with app.app_context():
        from app.models import Setting
        setting = Setting.query.first()
        token = (setting.telegram_bot_token or '').strip() if setting else ''
    if not token:
        logger.warning("Bot token не задан — бот не запущен (задай в админке)")
        return None
    old_thread = None
    with _bot_lock:
        if _bot is not None and _bot.token == token and \
                _bot._poll_thread and _bot._poll_thread.is_alive():
            return _bot
        if _bot is not None:
            _bot.stop_polling()
            old_thread = _bot._poll_thread
        _bot = TelegramBot(
            token, app,
            api_base=app.config['TELEGRAM_API_BASE'],
            proxy=app.config['TELEGRAM_PROXY'],
            timeout=app.config['TELEGRAM_TIMEOUT'],
            poll_timeout=app.config['TELEGRAM_POLL_TIMEOUT'],
        )
        # отправители со старыми токенами больше не нужны
        for stale_token in [t for t in _senders if t != token]:
            _senders.pop(stale_token, None)
    if old_thread is not None and old_thread.is_alive():
        # ждём, пока старый getUpdates завершится, иначе Telegram отдаст 409
        # на два поллера с одним токеном
        old_thread.join(timeout=app.config['TELEGRAM_POLL_TIMEOUT'] + 10)
    me = _bot.get_me()
    if me:
        logger.info(f"Бот: @{me.get('username')} ({me.get('first_name')})")
    else:
        logger.error("Не удалось получить getMe — проверь токен/прокси (TELEGRAM_PROXY)")
    _bot.start_polling(app)
    return _bot


def stop_bot():
    global _bot
    if _bot is not None:
        _bot.stop_polling()
        thread = _bot._poll_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=_bot.poll_timeout + 10)
        _bot = None


# ------------------------------------------------- готовые тексты уведомлений

def notify_new_works(sender, chat_id, works: list):
    if not works:
        return
    lines = ["<b>Новые работы в Timeline:</b>\n"]
    for w in works:
        if w.get('is_overdue'):
            lines.append(f"💀 Просрочена работа <b>{html.escape(w['discipline'])}</b>")
            lines.append("  ⚠️ Срок сдачи уже прошёл!")
        else:
            lines.append(f"• <b>{html.escape(w['discipline'])}</b>")
        if w.get('deadline'):
            lines.append(f"  Срок: {w['deadline']}")
        lines.append(f"  Статус: {w.get('status') or 'Не отправлена'}")
        lines.append("")
    lines.append(joke_html('new_works'))
    sender.send_message(chat_id, "\n".join(lines))


def notify_status_changes(sender, chat_id, changes: list):
    if not changes:
        return
    lines = ["<b>Изменения статуса работ:</b>\n"]
    for c in changes:
        if c.get('is_overdue'):
            lines.append(f"💀 Просрочена: <b>{html.escape(c['discipline'])}</b>")
        else:
            lines.append(f"• <b>{html.escape(c['discipline'])}</b>")
        if c.get('deadline'):
            lines.append(f"  Срок: {c['deadline']}")
        lines.append(f"  Статус: {c['old_status'] or 'Не отправлена'} → "
                     f"<b>{c['new_status'] or 'Не отправлена'}</b>")
        lines.append("")
    lines.append(joke_html('status_change'))
    sender.send_message(chat_id, "\n".join(lines))


def notify_deadline_reminders(sender, chat_id, groups: dict):
    labels = {
        48: '⏳ Осталось 48 часов до дедлайна',
        24: '⏳ Внимание: остался 1 день до дедлайна',
        12: '⚠️ Срочно: осталось менее 12 часов до дедлайна!',
    }
    for hours in (48, 24, 12):
        works = groups.get(hours) or []
        if not works:
            continue
        lines = [f"<b>{labels[hours]}</b>\n"]
        for w in works:
            lines.append(f"• <b>{html.escape(w['discipline'])}</b>")
            lines.append(f"  Срок: {w['deadline']}")
            lines.append("")
        lines.append(joke_html('reminder'))
        sender.send_message(chat_id, "\n".join(lines))


def notify_overdue(sender, chat_id, works: list):
    if not works:
        return
    lines = ["<b>💀 ПРОСРОЧЕННЫЕ РАБОТЫ:</b>\n"]
    for w in works:
        lines.append(f"• <b>{html.escape(w['discipline'])}</b>")
        lines.append(f"  Дедлайн был: {w['deadline']}")
        lines.append("")
    lines.append(joke_html('overdue_alert'))
    sender.send_message(chat_id, "\n".join(lines))


def notify_error(sender, chat_id, text: str):
    sender.send_message(chat_id, f"{text}\n\n{joke_html('error')}")


def notify_admin(sender, admin_chat_id, text: str):
    """Служебное сообщение админу (проблемы сайта/парсера)."""
    sender.send_message(admin_chat_id, f"🛠 {text}\n\n{joke_html('error')}")
