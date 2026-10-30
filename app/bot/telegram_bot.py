import html
import logging
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Any

from flask import has_app_context
import requests

from app.bot.jokes import joke, joke_html
from app.services import (
    COMPLETED_STATUSES,
    NOT_SENT_STATUSES,
    enrich_work_item,
    extract_file_attachments,
    extract_urls,
    format_diagnostic_block,
    is_work_overdue,
)

logger = logging.getLogger(__name__)


def is_maintenance_mode(app=None) -> bool:
    """Проверяет, активен ли глобальный режим технических работ (R4, AC 56)."""
    try:
        from app.models import Setting
        if app is not None and not has_app_context():
            with app.app_context():
                s = Setting.query.first()
                return bool(s and getattr(s, 'maintenance_mode', False))
        s = Setting.query.first()
        return bool(s and getattr(s, 'maintenance_mode', False))
    except Exception:
        return False

HELP_TEXT = (
    "<b>Что я умею</b>\n"
    "📅 Расписание — динамическое расписание пар УрГПУ (uspu.ru)\n"
    "📊 Статус — все работы, темы, баллы и дедлайны Timeline\n"
    "🔮 Гороскоп — студенческий астропрогноз на каждый день\n"
    "📋 На сегодня — дедлайны на сегодня и завтра\n"
    "💀 Просрочки — неотправленные просроченные работы\n"
    "🔄 Проверить — зайти в Timeline прямо сейчас\n"
    "⚙️ Профиль — группа, логин, уведомления, интервал\n"
    "❓ Помощь — эта справка\n\n"
    "Кнопки — внизу экрана. Команды:\n"
    "/schedule — расписание занятий\n"
    "/group МКТ-2531 — сменить учебную группу\n"
    "/status — работы, баллы и подробности\n"
    "/task ID — подробная карточка задания по ID\n"
    "/find слово — поиск заданий по теме и тексту\n"
    "/horoscope — студенческий гороскоп дня\n"
    "/stats — общая статистика\n"
    "/today — горящие работы (сегодня/завтра)\n"
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
BTN_HOROSCOPE = '🔮 Гороскоп'

# NEW v2.1/v3.0/v3.2: Фурри/фембой лейблы (R5)
BTN_STATUS_FEMBOY = '🐾 Статусик :3'
BTN_OVERDUE_FEMBOY = '🐾 Хвостики-долги'
BTN_CHECK_FEMBOY = '🐾 Потыкать лапкой'
BTN_PROFILE_FEMBOY = '🎀 Профиль :з'
BTN_SCHEDULE_FEMBOY = '📅 Расписаньице :3'
BTN_HOROSCOPE_FEMBOY = '🐾 Гороскоп :3'
BTN_TODAY_FEMBOY = '🐾 На сегодня :3'
BTN_HELP_FEMBOY = '🐾 Хелп :3'

HELP_TEXT_FEMBOY = (
    "🐾 <b>Что я умею :3</b>\n"
    "📅 Расписаньице — динамическое расписание пар УрГПУ (uspu.ru)\n"
    "🐾 Статусик — все заданьица, темы, балльчики и дедлайнчики Timeline\n"
    "🐾 Гороскоп — кавайный студенческий астропрогноз на каждый день :3\n"
    "🐾 На сегодня — горящие дедлайнчики на сегодня и завтра\n"
    "🐾 Хвостики-долги — неотправленные работы, требующие внимания лапок\n"
    "🐾 Потыкать лапкой — зайти в Timeline прямо сейчас\n"
    "🎀 Профиль :з — группка, логинчик, уведомлялочки и переключатели\n"
    "🐾 Хелп :3 — эта милая справочка\n\n"
    "Кнопочки — внизу экрана. Команды:\n"
    "/schedule — расписаньице пар\n"
    "/group МКТ-2531 — сменить учебную группку\n"
    "/status — заданьица, балльчики и подробности\n"
    "/task ID — подробная карточка заданьица по ID\n"
    "/find слово — поиск заданьиц по теме и тексту\n"
    "/horoscope — гороскопчик дня :3\n"
    "/stats — общая статистика\n"
    "/today — горящие заданьица (сегодня/завтра)\n"
    "/feedback текст — написать создателю бота\n"
    "/overdue — хвостики-долги\n"
    "/check — проверить Timeline лапкой\n"
    "/interval 15 — интервал проверок (мин)\n"
    "/login — сменить логин/пароль ЭИОС\n"
    "/unlink — удалить свои данные :(\n"
    "/cancel — отмена ввода."
)

# --- список работ постранично ---
STATUS_PAGE_SIZE = 4
SORT_NEW = 'new'        # новые работы (по дате появления) сверху
SORT_DEADLINE = 'dl'    # по срокам: что горит — выше

SORT_TITLES = {
    SORT_NEW: 'новые сверху, старьё в конце',
    SORT_DEADLINE: 'по срокам: сначала то, что горит',
}

SORT_TITLES_FEMBOY = {
    SORT_NEW: 'сначала новенькие :3',
    SORT_DEADLINE: 'по срокам: горящие дедлайнчики :3',
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


def _status_icon(status: str, is_overdue: bool, is_femboy: bool = False) -> str:
    """Эмодзи-иконка для статуса работы."""
    if is_overdue:
        return '😿' if is_femboy else '💀'
    s = (status or '').lower()
    if any(k in s for k in ('проверена', 'зачтена', 'отлично', 'хорошо', 'удовлетворит', 'принята')):
        return '🎀' if is_femboy else '✅'
    if 'отправлена' in s and 'не ' not in s:
        return '🐾' if is_femboy else '📬'
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
        self._admin_state = {}

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

    def answer_callback(self, callback_id: str, text: str = '', show_alert: bool = False, **kwargs):
        payload = {'callback_query_id': callback_id, 'text': text}
        if show_alert:
            payload['show_alert'] = True
        payload.update(kwargs)
        return self._api('answerCallbackQuery', payload)

    # --------------------------------------------------------------- клавиши

    def is_maintenance_mode(self) -> bool:
        return is_maintenance_mode(self.app)

    @staticmethod
    def _main_keyboard(user=None) -> dict:
        """Постоянное меню внизу экрана (reply keyboard). Поддерживает фембой-тему."""
        is_femboy = False
        if user is not None:
            if isinstance(user, dict):
                is_femboy = bool(user.get('theme_femboy', False))
            else:
                is_femboy = bool(getattr(user, 'theme_femboy', False))

        if is_femboy:
            b_status = BTN_STATUS_FEMBOY
            b_overdue = BTN_OVERDUE_FEMBOY
            b_check = BTN_CHECK_FEMBOY
            b_profile = BTN_PROFILE_FEMBOY
            b_schedule = BTN_SCHEDULE_FEMBOY
            b_horo = BTN_HOROSCOPE_FEMBOY
            b_today = BTN_TODAY_FEMBOY
            b_help = BTN_HELP_FEMBOY
            placeholder = 'Выбери лапкой :3'
        else:
            b_status = BTN_STATUS
            b_overdue = BTN_OVERDUE
            b_check = BTN_CHECK
            b_profile = BTN_PROFILE
            b_schedule = BTN_SCHEDULE
            b_horo = BTN_HOROSCOPE
            b_today = BTN_TODAY
            b_help = BTN_HELP
            placeholder = 'Выбери пункт меню'

        return {
            'keyboard': [
                [{'text': b_status}, {'text': b_overdue}],
                [{'text': b_check}, {'text': b_profile}],
                [{'text': b_schedule}, {'text': b_horo}],
                [{'text': b_today}, {'text': b_help}],
            ],
            'resize_keyboard': True,
            'is_persistent': True,
            'input_field_placeholder': placeholder,
        }

    @staticmethod
    def _profile_keyboard(user) -> dict:
        group_lbl = user.current_group if hasattr(user, 'current_group') else 'МКТ-2531'
        horo_notify = getattr(user, 'horoscope_notify', False)
        zodiac_key = getattr(user, 'zodiac_sign', None)
        from app.bot.horoscope import ZODIAC_SIGNS
        sign_info = ZODIAC_SIGNS.get(zodiac_key) if zodiac_key else None
        sign_label = f"{sign_info['symbol']} {sign_info['name']}" if sign_info else 'не выбран'
        is_femboy = bool(user and getattr(user, 'theme_femboy', False))

        if is_femboy:
            group_btn = f"👥 Группка: {group_lbl} :3"
            relogin_btn = '🔑 Сменить логин/пароль ЭИОС'
            notify_btn = ('🔕 Выключить уведомления :3' if user.notify_enabled
                          else '🔔 Включить уведомления :3')
            sched_btn = ('📅 Расписаньице на завтра: ВКЛ ✅ :3' if user.schedule_notify
                         else '📅 Расписаньице на завтра: ВЫКЛ')
            digest_btn = ('🌅 Дайджестик утром: ВКЛ ✅ :3' if user.digest_notify
                          else '🌅 Дайджестик утром: ВЫКЛ')
            horo_btn = (f'🐾 Гороскопчик в дайджесте: ВКЛ ({sign_label}) ✅' if horo_notify
                        else '🐾 Гороскопчик в дайджесте: ВЫКЛ')
            unlink_btn = '🗑 Удалить мои данные :('
        else:
            group_btn = f"👥 Группа: {group_lbl}"
            relogin_btn = '🔑 Сменить логин/пароль ЭИОС'
            notify_btn = ('🔕 Выключить уведомления' if user.notify_enabled
                          else '🔔 Включить уведомления')
            sched_btn = ('📅 Расписание на завтра: ВКЛ ✅' if user.schedule_notify
                         else '📅 Расписание на завтра: ВЫКЛ')
            digest_btn = ('🌅 Дайджест утром: ВКЛ ✅' if user.digest_notify
                          else '🌅 Дайджест утром: ВЫКЛ')
            horo_btn = (f'🔮 Гороскоп в дайджесте: ВКЛ ({sign_label}) ✅' if horo_notify
                        else '🔮 Гороскоп в дайджесте: ВЫКЛ')
            unlink_btn = '🗑 Удалить мои данные'

        rows = [
            [{'text': group_btn, 'callback_data': 'change_group'}],
            [{'text': relogin_btn, 'callback_data': 'relogin'}],
            [{'text': notify_btn, 'callback_data': 'toggle_notify'}],
            [{'text': sched_btn, 'callback_data': 'toggle_schedule_notify'}],
            [{'text': digest_btn, 'callback_data': 'toggle_digest_notify'}],
            [{'text': horo_btn, 'callback_data': 'toggle_horoscope_notify'}],
        ]
        if horo_notify:
            change_sign_btn = f"🐾 Сменить знак: {sign_label} :3" if is_femboy else f"♈ Сменить знак: {sign_label}"
            rows.append([{'text': change_sign_btn, 'callback_data': 'horo_pick_sign'}])

        if getattr(user, 'femboy_unlocked', False):
            is_active = getattr(user, 'theme_femboy', False)
            btn_txt = '🐾 Тема: ВКЛ :3' if is_active else '🐾 Тема: ВЫКЛ'
            rows.append([{'text': btn_txt, 'callback_data': 'toggle_theme_femboy'}])
        rows.append([{'text': unlink_btn, 'callback_data': 'unlink'}])
        return {'inline_keyboard': rows}

    @staticmethod
    def _horoscope_selection_keyboard(prefix: str = 'horo_view', user=None, back_to_profile: bool = False) -> dict:
        """Сетка 4x3 для выбора знака зодиака."""
        from app.bot.horoscope import ZODIAC_SIGNS
        is_femboy = bool(user and getattr(user, 'theme_femboy', False))
        signs = list(ZODIAC_SIGNS.items())
        rows = []
        for i in range(0, len(signs), 3):
            row = []
            for key, data in signs[i:i+3]:
                label = f"{data['symbol']} {data['femboy_name'].split()[0] if is_femboy else data['name']}"
                row.append({'text': label, 'callback_data': f"{prefix}|{key}"})
            rows.append(row)
        if back_to_profile:
            rows.append([{'text': '⬅️ Назад в профиль', 'callback_data': 'profile'}])
        return {'inline_keyboard': rows}

    @staticmethod
    def _horoscope_card_keyboard(sign: str, user=None) -> dict:
        """Кнопки под карточкой гороскопа: выбор другого знака и привязка к дайджесту."""
        from app.bot.horoscope import ZODIAC_SIGNS
        is_femboy = bool(user and getattr(user, 'theme_femboy', False))
        user_sign = getattr(user, 'zodiac_sign', None)
        rows = []
        if user_sign != sign:
            sym = ZODIAC_SIGNS.get(sign, {}).get('symbol', '⭐')
            save_lbl = f"{sym} Привязать к утреннему дайджесту" if not is_femboy else f"🐾 Привязать {sym} к дайджесту :3"
            rows.append([{'text': save_lbl, 'callback_data': f"horo_save_digest|{sign}"}])
        rows.append([
            {'text': '🔮 Другой знак' if not is_femboy else '🐾 Другой знак :3', 'callback_data': 'horo_menu'},
            {'text': '⚙️ Профиль' if not is_femboy else '🎀 Профиль :з', 'callback_data': 'profile'},
        ])
        return {'inline_keyboard': rows}

    @staticmethod
    def _schedule_keyboard(group_name: str, mode: str = 'today', offset_weeks: int = 0, is_femboy: bool = False) -> dict:
        btn_today = {
            'text': '• Сегодня •' if mode == 'today' and offset_weeks == 0 else ('📍 Сегодня :3' if is_femboy else '📍 Сегодня'),
            'callback_data': 'sch|today|0'
        }
        btn_tomorrow = {
            'text': '• Завтра •' if mode == 'tomorrow' else ('⏩ Завтра :3' if is_femboy else '⏩ Завтра'),
            'callback_data': 'sch|tomorrow|0'
        }
        btn_week = {
            'text': '• Вся неделя •' if mode == 'week' and offset_weeks == 0 else ('📆 Вся неделька :3' if is_femboy else '📆 Вся неделя'),
            'callback_data': 'sch|week|0'
        }

        prev_offset = offset_weeks - 1
        next_offset = offset_weeks + 1

        if mode == 'week':
            week_label = ('Текущая неделька :3' if is_femboy else 'Текущая нед.') if offset_weeks == 0 else f'Неделя {offset_weeks:+d}'
            nav_row = [
                {'text': '◀ Пред. нед.' if is_femboy else '◀ Пред. неделя', 'callback_data': f'sch|week|{prev_offset}'},
                {'text': f'• {week_label} •', 'callback_data': f'sch|week|{offset_weeks}'},
                {'text': 'След. нед. ▶' if is_femboy else 'След. неделя ▶', 'callback_data': f'sch|week|{next_offset}'},
            ]
        else:
            nav_row = [
                {'text': '◀ Пред. нед.' if is_femboy else '◀ Пред. неделя', 'callback_data': 'sch|week|-1'},
                {'text': '🐾 Обновить' if is_femboy else '🔄 Обновить', 'callback_data': f'sch|refresh|{offset_weeks}'},
                {'text': 'След. нед. ▶' if is_femboy else 'След. неделя ▶', 'callback_data': 'sch|week|1'},
            ]

        grp_label = f'👥 Группка: {group_name} :3' if is_femboy else f'👥 Группа: {group_name}'
        return {
            'inline_keyboard': [
                [btn_today, btn_tomorrow, btn_week],
                nav_row,
                [{'text': grp_label, 'callback_data': 'change_group'}],
            ]
        }

    @staticmethod
    def _admin_keyboard(maintenance_mode: bool | None = None) -> dict:
        if maintenance_mode is None:
            try:
                from app.models import Setting
                s = Setting.query.first()
                maintenance_mode = bool(s and getattr(s, 'maintenance_mode', False))
            except Exception:
                maintenance_mode = False

        maint_btn = (
            [{'text': '🟢 Выключить тех. работы', 'callback_data': 'admin_maint_off'}]
            if maintenance_mode else
            [{'text': '🔴 Включить режим тех. работ?', 'callback_data': 'admin_maint_prompt'}]
        )

        return {
            'inline_keyboard': [
                [{'text': '🔄 Проверить всех', 'callback_data': 'admin_check'}],
                [
                    {'text': '🩺 БД', 'callback_data': 'admin_health_db'},
                    {'text': '⏱ Планировщик', 'callback_data': 'admin_health_sched'},
                ],
                [
                    {'text': '🌐 ЭИОС', 'callback_data': 'admin_health_eios'},
                    {'text': '🤖 Telegram API', 'callback_data': 'admin_health_tg'},
                ],
                [
                    {'text': '✉️ Написать юзеру', 'callback_data': 'admin_msg_pick'},
                    {'text': '📢 Рассылка', 'callback_data': 'admin_broadcast_prompt'},
                ],
                maint_btn,
            ]
        }

    @staticmethod
    def _norm(text: str) -> str:
        """'📊 Статус' -> 'статус' (чтобы кнопки и команды матчились одинаково)."""
        cleaned = ''.join(ch for ch in (text or '').lower()
                         if ch.isalnum() or ch in ' /_.-').strip()
        aliases = {
            'статусик 3': 'статус',
            'статусик з': 'статус',
            'статусик': 'статус',
            'хвостики-долги': 'просрочки',
            'хвостики долги': 'просрочки',
            'хвостики': 'просрочки',
            'потыкать лапкой': 'проверить',
            'потыкать': 'проверить',
            'лапкой': 'проверить',
            'профиль з': 'профиль',
            'профиль 3': 'профиль',
            'расписаньице 3': 'расписание',
            'расписаньице з': 'расписание',
            'расписаньице': 'расписание',
            'долги': 'просрочки',
        }
        return aliases.get(cleaned, cleaned)

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
                + joke_html('start', user=user),
                reply_markup=self._main_keyboard(user),
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
            reply(text, self._main_keyboard(user))

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
                          + joke_html('unlink', user=user))

    def _menu(self, chat_id: str, user=None):
        if user is None:
            user = self._get_user(chat_id)
        self.send_message(chat_id, "Меню — кнопки внизу 👇\n\n" + joke_html('help', user=user),
                          reply_markup=self._main_keyboard(user))

    def _help_text(self, user=None) -> str:
        is_femboy = bool(user and getattr(user, 'theme_femboy', False))
        text = HELP_TEXT_FEMBOY if is_femboy else HELP_TEXT
        return text + "\n\n" + joke_html('help', user=user)

    # ------------------------------------------------------------ расписание

    def _cmd_schedule(self, user, chat_id: str, mode: str = 'today',
                      force_refresh: bool = False, message_id: int = None,
                      offset_weeks: int = 0):
        from app.parser.schedule import get_schedule_view
        group = getattr(user, 'current_group', 'МКТ-2531')
        is_femboy = bool(user and getattr(user, 'theme_femboy', False))
        try:
            text = get_schedule_view(group, mode=mode, force_refresh=force_refresh,
                                     offset_weeks=offset_weeks, is_femboy=is_femboy)
        except Exception as e:
            logger.error(f"Ошибка загрузки расписания: {e}")
            err_title = (f"⚠️ Не удалось загрузить расписаньице для группки <b>{html.escape(group)}</b> :3\n"
                         if is_femboy else
                         f"⚠️ Не удалось загрузить расписание для группы <b>{html.escape(group)}</b>.\n")
            text = (err_title + f"Причина: <code>{html.escape(str(e))}</code>")

        keyboard = self._schedule_keyboard(group, mode=mode, offset_weeks=offset_weeks, is_femboy=is_femboy)
        if message_id:
            if not self.edit_message(chat_id, message_id, text, keyboard):
                # Если edit_message не смог изменить (например, удалено пользователем)
                if self._last_error and 'not modified' not in self._last_error.lower():
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
        user.group_manually_set = True
        user.state = 'ready' if user.is_registered else 'login'
        db.session.commit()
        self.send_message(
            chat_id,
            f"✅ Твоя академическая группа установлена: <b>{html.escape(canonical)}</b>.",
            reply_markup=self._main_keyboard(user)
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

    def _status_keyboard(self, page: int, pages: int, sort: str, chunk: list = None,
                         start_idx: int = 1, is_femboy: bool = False) -> dict:
        rows = []
        if chunk:
            action_row = []
            for offset, it in enumerate(chunk):
                num = start_idx + offset
                ext_id = str(getattr(it, 'external_id', None) or (it.get('external_id') if isinstance(it, dict) else getattr(it, 'id', '')))
                has_note = bool(getattr(it, 'teacher_note', None) or (isinstance(it, dict) and it.get('teacher_note')))
                if is_femboy:
                    icon = '🎀' if has_note else '🐾'
                else:
                    icon = '💬' if has_note else '📋'
                action_row.append({
                    'text': f"{icon} #{num}",
                    'callback_data': f"wv|{ext_id}|{sort}|{page}",
                })
            rows.append(action_row)

        nav = []
        if page > 1:
            nav.append({'text': '⬅️ Назад', 'callback_data': f'st|{sort}|{page - 1}'})
        nav.append({'text': f'стр. {page}/{pages}', 'callback_data': 'noop'})
        if page < pages:
            nav.append({'text': 'Вперёд ➡️', 'callback_data': f'st|{sort}|{page + 1}'})
        rows.append(nav)

        other = SORT_DEADLINE if sort == SORT_NEW else SORT_NEW
        if is_femboy:
            toggle = ('⏰ Дедлайнчики' if sort == SORT_NEW else '🆕 Новенькие :3')
            compact_btn = '🔽 Компактно :3'
        else:
            toggle = ('⏰ По срокам' if sort == SORT_NEW else '🆕 Новые сверху')
            compact_btn = '🔽 Компактно'
        rows.append([
            {'text': toggle, 'callback_data': f'st|{other}|1'},
            {'text': compact_btn, 'callback_data': 'st_compact'},
        ])
        return {'inline_keyboard': rows}

    def _build_status_page(self, user, page: int = 1,
                           sort: str = SORT_NEW) -> tuple[str, dict | None]:
        """Страница списка работ: текст + клавиатура листалки."""
        if self.app is not None and not has_app_context():
            with self.app.app_context():
                return self._build_status_page(user, page, sort)
        from app.services import enrich_work_item as _enrich, format_diagnostic_block

        items = self._status_items(user, sort)
        streak = getattr(user, 'fail_streak', 0) or 0
        show_error = bool(user.last_error and streak >= 3)
        is_femboy = bool(getattr(user, 'theme_femboy', False))
        sort_titles = SORT_TITLES_FEMBOY if is_femboy else SORT_TITLES

        if not items:
            empty_lines = []
            if is_femboy:
                empty_lines.append("<code>ПЕРВОЕ ПРАВИЛО ФЕМБОЙ ТЕМЫ — НИКОМУ НЕ ГОВОРИТЬ О ФЕМБОЙ ТЕМЕ</code>\n")
                empty_lines.append("<i>Заданьиц пока нет :3 Нажми «🐾 Потыкать лапкой».</i>")
                empty_lines.append("Если это повторяется — проверь логин/пароль: /login")
                empty_lines.append("")
            else:
                empty_lines.append("<i>Работ пока нет. Нажми «🔄 Проверить».</i>")
                empty_lines.append("Если это повторяется — проверь логин/пароль: /login")
                empty_lines.append("")
            if show_error:
                diag = format_diagnostic_block('TimelineScraper', user.last_error, f"{streak}/3")
                empty_lines.append(f"⚠️ Последняя проверка не удалась: <code>{html.escape(user.last_error)}</code>")
                empty_lines.append(diag)
                empty_lines.append(joke_html('apology', user=user))
                empty_lines.append("")
            empty_lines.append(joke_html('empty', user=user))
            return "\n".join(empty_lines), None

        pages = max(1, (len(items) + STATUS_PAGE_SIZE - 1) // STATUS_PAGE_SIZE)
        page = min(max(1, page), pages)
        start = (page - 1) * STATUS_PAGE_SIZE
        chunk = items[start:start + STATUS_PAGE_SIZE]

        lines = []
        if is_femboy:
            lines.append("<code>ПЕРВОЕ ПРАВИЛО ФЕМБОЙ ТЕМЫ — НИКОМУ НЕ ГОВОРИТЬ О ФЕМБОЙ ТЕМЕ</code>\n")
            lines.append(f"<b>🐾 Лапки-заданьица — стр. {page}/{pages}</b>  <i>(всего {len(items)}, "
                         f"{sort_titles.get(sort, '')})</i>")
        else:
            lines.append(f"<b>📊 Работы — стр. {page}/{pages}</b>  <i>(всего {len(items)}, "
                         f"{sort_titles.get(sort, '')})</i>")
        lines.append(f"<code>{self._silly_bar(page, pages)}</code>")
        lines.append("")

        for offset, it in enumerate(chunk):
            e = _enrich(it)
            icon = _status_icon(e['status'], e['is_overdue'], is_femboy=is_femboy)
            lines.append(f"{icon} <b>{start + offset + 1}. {html.escape(e['discipline'])}</b>")
            if e.get('point_name'):
                topic_lbl = "🐾 Тема заданьица:" if is_femboy else "📌 Тема:"
                lines.append(f"   {topic_lbl} {html.escape(e['point_name'])}")
            if e['deadline']:
                countdown = _format_countdown(e['deadline'])
                dl_lbl = "⏳ Дедлайнчик:" if is_femboy else "⏳ Срок:"
                lines.append(f"   {dl_lbl} {e['deadline']}" + (f'  ({countdown})' if countdown else ''))
            detected_files = extract_file_attachments(f"{e.get('control_text', '')} {e.get('teacher_note', '')}")
            if detected_files:
                file_badge = ", ".join(f"<code>{html.escape(f)}</code>" for f in detected_files[:2])
                files_lbl = "🐾 Файлики:" if is_femboy else "📁 Файл:"
                lines.append(f"   {files_lbl} {file_badge}")

            status_val = e['status']
            if is_femboy:
                if status_val in ('', 'Не отправлена'):
                    status_val = 'Не сдано лапками :3'
                elif 'отправлена' in status_val.lower():
                    status_val = 'Сдано лапками :3'
                elif any(k in status_val.lower() for k in ('зачтена', 'проверена', 'принята')):
                    status_val = 'Зачтено :3'

            if e.get('points'):
                pts_lbl = "🎯 Балльчики:" if is_femboy else "🎯 Баллы:"
                lines.append(f"   {pts_lbl} {html.escape(e['points'])} | Статус: {status_val}")
            else:
                lines.append(f"   Статус: {status_val}")
            lines.append("")

        if show_error:
            diag = format_diagnostic_block('TimelineScraper', user.last_error, f"{streak}/3")
            lines.append(f"⚠️ Последняя проверка не удалась: <code>{html.escape(user.last_error)}</code>")
            lines.append(diag)
            lines.append(joke_html('apology', user=user))
            lines.append("")

        lines.append(joke_html('status_last' if page == pages else 'status', page - 1, user=user))

        return "\n".join(lines), self._status_keyboard(
            page, pages, sort, chunk=chunk, start_idx=start + 1,
            is_femboy=getattr(user, 'theme_femboy', False)
        )

    def _build_work_detail(self, user, external_id: str, sort: str = SORT_NEW, page: int = 1) -> tuple[str, dict]:
        """Экран детального просмотра работы: тема, задание, баллы, рецензия преподавателя."""
        if self.app is not None and not has_app_context():
            with self.app.app_context():
                return self._build_work_detail(user, external_id, sort, page)

        from app.models import WorkItem
        from app.services import enrich_work_item as _enrich

        item = WorkItem.query.filter_by(user_id=user.id, external_id=str(external_id)).first()
        if not item and str(external_id).isdigit():
            item = WorkItem.query.filter_by(user_id=user.id, id=int(external_id)).first()

        # Автоматическая синхронизация при первом открытии после миграции v3.1, если поля еще NULL
        if item and item.control_text is None and item.point_name is None:
            try:
                from app.scheduler.jobs import check_user
                check_user(self.app, user.id, force=True, notify=False)
                item = WorkItem.query.filter_by(user_id=user.id, external_id=str(external_id)).first() or item
            except Exception as ex:
                logger.warning(f"Ошибка автосинхронизации задания {external_id}: {ex}")

        is_femboy = bool(getattr(user, 'theme_femboy', False))

        if not item:
            err_text = "😿 Заданьице не найдено :3" if is_femboy else "⚠️ Задание не найдено в базе бота."
            kb = {'inline_keyboard': [[{'text': '⬅️ Назад к списку', 'callback_data': f'st|{sort}|{page}'}]]}
            return err_text, kb

        e = _enrich(item)
        icon = _status_icon(e['status'], e['is_overdue'], is_femboy=is_femboy)
        countdown = _format_countdown(e['deadline']) if e.get('deadline') else ''

        lines = [
            f"{icon} <b>{html.escape(e['discipline'])}</b>",
            "",
        ]

        if e.get('point_name'):
            topic_label = "📌 <b>Тема:</b>" if not is_femboy else "🐾 <b>Тема заданьица:</b>"
            lines.append(f"{topic_label} {html.escape(e['point_name'])}")

        if e.get('deadline'):
            dl_label = "⏳ <b>Срок сдачи:</b>" if not is_femboy else "⏳ <b>Дедлайнчик:</b>"
            cd_str = f" <i>({countdown})</i>" if countdown else ""
            lines.append(f"{dl_label} {e['deadline']}{cd_str}")

        status_text = e['status']
        if is_femboy:
            if status_text in ('', 'Не отправлена'):
                status_text = 'Не сдано лапками :3'
            elif 'отправлена' in status_text.lower():
                status_text = 'Сдано лапками :3'
            elif any(k in status_text.lower() for k in ('зачтена', 'проверена', 'принята')):
                status_text = 'Зачтено :3'
            status_label = "🐾 <b>Статусик:</b>"
        else:
            status_label = "📊 <b>Статус:</b>"
        lines.append(f"{status_label} {status_text}")

        if e.get('points'):
            pts_label = "🎯 <b>Баллы:</b>" if not is_femboy else "🎯 <b>Балльчики:</b>"
            lines.append(f"{pts_label} <code>{html.escape(e['points'])}</code>")

        control_text = e.get('control_text') or ''
        teacher_note = e.get('teacher_note') or ''
        combined_text = f"{control_text} {teacher_note}"
        extracted_files = extract_file_attachments(combined_text)
        extracted_urls = extract_urls(combined_text)

        if extracted_files:
            files_label = "📁 <b>Файлы к заданию:</b>" if not is_femboy else "🐾 <b>Файлики к заданьицу:</b>"
            files_badges = " ".join(f"<code>{html.escape(f)}</code>" for f in extracted_files)
            lines.append(f"{files_label} {files_badges}")
        elif extracted_urls:
            urls_label = "🔗 <b>Материалы по ссылке:</b>" if not is_femboy else "🎀 <b>Материальчики:</b>"
            urls_badges = ", ".join(f'<a href="{html.escape(u)}">{html.escape(u[:40])}…</a>' if len(u) > 40 else f'<a href="{html.escape(u)}">{html.escape(u)}</a>' for u in extracted_urls)
            lines.append(f"{urls_label} {urls_badges}")
        else:
            files_label = "📎 <b>Вложение:</b>" if not is_femboy else "🐾 <b>Файлик:</b>"
            if e['status'] in ('Отправлена', 'Проверена', 'Зачтена'):
                lines.append(f"{files_label} <i>работа сдана через Timeline (просмотр в веб-версии)</i>" if not is_femboy else f"{files_label} <i>сдано в Timeline :3</i>")
            else:
                lines.append(f"{files_label} <i>не прикреплено (задание сформулировано текстом)</i>" if not is_femboy else f"{files_label} <i>не прикреплён (заданьице текстом) :3</i>")

        if e.get('unavailability_reason'):
            lines.append("")
            lines.append(f"<blockquote>⚠️ <b>Доступность:</b> {html.escape(e['unavailability_reason'])}</blockquote>")

        lines.append("")

        task_label = "📋 <b>Задание преподавателя:</b>" if not is_femboy else "🐾 <b>Текст заданьица:</b>"
        if control_text:
            if len(control_text) > 3000:
                control_text = control_text[:2950] + "… <i>[текст обрезан по лимиту]</i>"
            lines.append(task_label)
            lines.append(f"<blockquote>{html.escape(control_text)}</blockquote>")

            clean_c = control_text.strip().rstrip('.').lower()
            if clean_c in ('выполнить работу', 'выполнить задание', 'работа', 'задание'):
                hint = ("💡 <i>В поле задания указано «Выполнить работу». Сами материалы и файлы лекции прикреплены в графике Timeline (кнопка ниже).</i>"
                        if not is_femboy else
                        "🐾 <i>В заданьице написано «Выполнить работу». Файлики и методички прикреплены в графике Timeline (лапками по кнопочке ниже) :3</i>")
                lines.append(hint)
        else:
            hint = ("<i>В поле задания формулировка не заполнена преподавателем. Материалы и файлы лекции прикреплены в графике Timeline (кнопка ниже).</i>"
                    if not is_femboy else
                    "<i>В заданьице буковки не написаны. Файлики ищи в графике Timeline :3</i>")
            lines.append(task_label)
            lines.append(f"<blockquote>{hint}</blockquote>")

        if teacher_note:
            lines.append("")
            note_label = "💬 <b>Рецензия преподавателя:</b>" if not is_femboy else "🎀 <b>Комментарик преподавателя:</b>"
            if len(teacher_note) > 1000:
                teacher_note = teacher_note[:950] + "… <i>[текст обрезан]</i>"
            lines.append(note_label)
            lines.append(f"<blockquote>{html.escape(teacher_note)}</blockquote>")

        btn_timeline = "📅 График в Timeline" if not is_femboy else "📅 График в Timeline :3"
        btn_send = "📤 Сдать работу" if not is_femboy else "🐾 Сдать работу :3"
        btn_back = "⬅️ Лапки назад" if is_femboy else "⬅️ Назад к списку"

        keyboard = {
            'inline_keyboard': [
                [
                    {'text': btn_timeline, 'url': 'https://timeline.rsvpu.ru/Timeline'},
                    {'text': btn_send, 'url': 'https://timeline.rsvpu.ru/StudentWork/Send'},
                ],
                [{'text': btn_back, 'callback_data': f'st|{sort}|{page}'}],
            ]
        }

        return "\n".join(lines), keyboard

    def _build_status_compact(self, user) -> tuple[str, dict]:
        """Компактная сводка по работам (без подробного списка)."""
        if self.app is not None and not has_app_context():
            with self.app.app_context():
                return self._build_status_compact(user)
        from app.models import WorkItem
        from app.services import enrich_work_item as _enrich

        is_femboy = bool(getattr(user, 'theme_femboy', False))
        items = WorkItem.query.filter_by(user_id=user.id).all()
        if not items:
            empty_compact = ("<code>ПЕРВОЕ ПРАВИЛО ФЕМБОЙ ТЕМЫ — НИКОМУ НЕ ГОВОРИТЬ О ФЕМБОЙ ТЕМЕ</code>\n\n<i>Заданьиц пока нет :3 Нажми «🐾 Потыкать лапкой».</i>"
                             if is_femboy else
                             "<i>Работ пока нет. Нажми «🔄 Проверить».</i>")
            return (empty_compact, None)

        enriched = [_enrich(it) for it in items]
        total = len(enriched)
        overdue_cnt = sum(1 for e in enriched if e['is_overdue'])
        sent_cnt = sum(1 for e in enriched if e['status'] not in ('', 'Не отправлена'))
        pending_cnt = total - sent_cnt - overdue_cnt

        if is_femboy:
            lines = [
                "<code>ПЕРВОЕ ПРАВИЛО ФЕМБОЙ ТЕМЫ — НИКОМУ НЕ ГОВОРИТЬ О ФЕМБОЙ ТЕМЕ</code>\n",
                "<b>🐾 Сводка заданьиц (компактный вид) :3</b>",
                "",
                f"Всего заданьиц: <b>{total}</b>",
                f"🎀 Сдано лапками: <b>{sent_cnt}</b>",
                f"⏳ В процессе: <b>{max(0, pending_cnt)}</b>",
                f"😿 Просрочено (хвостики): <b>{overdue_cnt}</b>",
                "",
                joke_html('status', 0, user=user)
            ]
            keyboard = {
                'inline_keyboard': [
                    [{'text': '📄 Подробный список :3', 'callback_data': 'st|new|1'}],
                ]
            }
        else:
            lines = [
                "<b>📊 Сводка работ (компактный вид)</b>",
                "",
                f"Всего: <b>{total}</b>",
                f"✅ Сдано: <b>{sent_cnt}</b>",
                f"⏳ В процессе: <b>{max(0, pending_cnt)}</b>",
                f"💀 Просрочено: <b>{overdue_cnt}</b>",
                "",
                joke_html('status', 0, user=user)
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
        if self.app is not None and not has_app_context():
            with self.app.app_context():
                return self._build_overdue_text(user)
        from datetime import datetime, timedelta
        from app.models import WorkItem
        from app.services import enrich_work_item as _enrich

        now = datetime.now()
        cutoff = now - timedelta(days=5)

        raw_items = WorkItem.query.filter_by(user_id=user.id).all()
        overdue = []
        for it in raw_items:
            e = _enrich(it)
            if not e.get('is_overdue'):
                continue

            dl = e.get('deadline')
            if not dl:
                continue

            dl_dt = None
            if isinstance(dl, datetime):
                dl_dt = dl
            else:
                try:
                    dl_dt = datetime.strptime(str(dl).strip(), '%d.%m.%Y %H:%M')
                except (ValueError, TypeError):
                    continue

            curr_cutoff = cutoff
            if curr_cutoff.tzinfo is not None and dl_dt.tzinfo is None:
                curr_cutoff = curr_cutoff.replace(tzinfo=None)
            elif curr_cutoff.tzinfo is None and dl_dt.tzinfo is not None:
                dl_dt = dl_dt.replace(tzinfo=None)

            if dl_dt < curr_cutoff:
                continue

            overdue.append(e)

        is_femboy = bool(getattr(user, 'theme_femboy', False))
        if not overdue:
            clean_msg = ("<i>Хвостиков-долгов нет! Лапки чистые, можно отдыхать! 🎉 :3</i>\n\n"
                         if is_femboy else
                         "<i>Просроченных неотправленных работ нет. Отличная работа! 👍</i>\n\n")
            return clean_msg + joke_html('overdue_clean', user=user)

        hdr = f"<b>🐾 Пушистые хвостики-долги ({len(overdue)}) :3:</b>\n" if is_femboy else f"<b>💀 Просроченные работы ({len(overdue)}):</b>\n"
        lines = [hdr]
        for e in overdue:
            lines.append(f"• <b>{html.escape(e['discipline'])}</b>")
            dl_lbl = "Дедлайнчик:" if is_femboy else "Срок:"
            lines.append(f"  {dl_lbl} {e['deadline']}")
            lines.append("")
        lines.append(joke_html('overdue', user=user))
        return "\n".join(lines)

    def _build_last_check_text(self, user) -> str:
        if self.app is not None and not has_app_context():
            with self.app.app_context():
                return self._build_last_check_text(user)
        from app.services import format_diagnostic_block

        if not user.last_check_at:
            return "<i>Проверка ещё не запускалась.</i>\n\n" + joke_html('last_check', user=user)
        ts = user.last_check_at.strftime('%d.%m.%Y %H:%M:%S')
        interval = user.check_interval or self.app.config.get('DEFAULT_CHECK_INTERVAL', 300)
        text = (f"<b>Последняя проверка:</b>\n{ts} UTC\n\n"
                f"Интервал: {interval // 60} мин\n")
        streak = getattr(user, 'fail_streak', 0) or 0
        if user.last_error and streak >= 3:
            diag = format_diagnostic_block('TimelineScraper', user.last_error, f"{streak}/3")
            text += (f"\n⚠️ Ошибка: <code>{html.escape(user.last_error)}</code>\n"
                     f"{diag}\n"
                     f"{joke_html('apology', user=user)}\n")
        return text + "\n" + joke_html('last_check', user=user)

    def _build_profile_text(self, user) -> str:
        if self.app is not None and not has_app_context():
            with self.app.app_context():
                return self._build_profile_text(user)
        from app.services import format_diagnostic_block

        interval = user.check_interval or self.app.config.get('DEFAULT_CHECK_INTERVAL', 300)
        group = getattr(user, 'current_group', 'МКТ-2531')
        schedule_notify_str = 'ВКЛ ✅' if getattr(user, 'schedule_notify', False) else 'ВЫКЛ'
        digest_notify_str = 'ВКЛ ✅' if getattr(user, 'digest_notify', False) else 'ВЫКЛ'

        horo_notify = getattr(user, 'horoscope_notify', False)
        zodiac_key = getattr(user, 'zodiac_sign', None)
        from app.bot.horoscope import ZODIAC_SIGNS
        sign_info = ZODIAC_SIGNS.get(zodiac_key) if zodiac_key else None
        sign_label = f"{sign_info['symbol']} {sign_info['name']}" if sign_info else 'не выбран'
        horo_notify_str = f"ВКЛ ({sign_label}) ✅" if horo_notify else f"ВЫКЛ (знак: {sign_label})"

        streak = getattr(user, 'fail_streak', 0) or 0
        error_section = ""
        if user.last_error and streak >= 3:
            diag = format_diagnostic_block('TimelineAuth', user.last_error, f"{streak}/3")
            error_section = (f"⚠️ Ошибка: <code>{html.escape(user.last_error)}</code>\n"
                             f"{diag}\n"
                             f"{joke_html('apology', user=user)}\n")

        is_femboy = bool(getattr(user, 'theme_femboy', False))
        if is_femboy:
            hdr = "<b>🎀 Профиль :з</b>\n"
            pwd_txt = 'зашифрован и спрятан в лапках :3' if user.password_enc else '—'
            sess_txt = 'лапки в тепле (активна) :3' if user.session_cookies else 'лапки замёрзли (не создана)'
            notif_txt = 'включены :3' if user.notify_enabled else 'выключены'
            last_check_lbl = 'Последний тык лапкой:'
            sched_lbl = 'Расписаньице на завтра:'
            digest_lbl = 'Дайджестик утром:'
            horo_lbl = 'Гороскопчик в дайджесте:'
            group_lbl = 'Академ. группка:'
        else:
            hdr = "<b>⚙️ Профиль</b>\n"
            pwd_txt = 'зашифрован и сохранён' if user.password_enc else '—'
            sess_txt = 'активна' if user.session_cookies else 'не создана'
            notif_txt = 'включены' if user.notify_enabled else 'выключены'
            last_check_lbl = 'Последняя проверка:'
            sched_lbl = 'Расписание на завтра:'
            digest_lbl = 'Дайджест утром:'
            horo_lbl = 'Гороскоп в дайджесте:'
            group_lbl = 'Академ. группа:'

        return (
            hdr
            + f"{group_lbl} <code>{html.escape(group)}</code> (/group)\n"
            f"Логин ЭИОС: <code>{html.escape(user.timeline_login or '—')}</code>\n"
            f"Пароль: {pwd_txt}\n"
            f"Сессия: {sess_txt}\n"
            f"Уведомления: {notif_txt}\n"
            f"{sched_lbl} {schedule_notify_str}\n"
            f"{digest_lbl} {digest_notify_str}\n"
            f"{horo_lbl} {horo_notify_str}\n"
            f"Интервал проверки: {interval // 60} мин\n"
            f"{last_check_lbl} "
            f"{user.last_check_at.strftime('%d.%m.%Y %H:%M') if user.last_check_at else '—'}\n"
            + error_section
            + "\n" + joke_html('profile', user=user)
        )

    # --------------------------------------------------------------- callback

    def _handle_callback(self, cb: dict):
        if self.app is not None and not has_app_context():
            with self.app.app_context():
                return self._handle_callback(cb)

        from app.models import Setting, User, WorkItem, db

        cb_id = cb.get('id')
        data = cb.get('data', '')
        msg = cb.get('message') or {}
        chat = msg.get('chat') or {}
        chat_id = str(chat.get('id', ''))
        message_id = msg.get('message_id')
        from_user = cb.get('from') or {}

        user = self._get_or_create_user(chat, from_user)

        admin_chat_id = self.app.config.get('ADMIN_CHAT_ID') if self.app else None
        is_admin = bool(admin_chat_id) and str(chat_id) == str(admin_chat_id)

        # 1. Intercept in maintenance mode for non-admin users
        if self.is_maintenance_mode() and not is_admin:
            self.answer_callback(
                cb_id,
                "🔧 Ведутся технические работы! Пожалуйста, попробуйте позже.",
                show_alert=True,
            )
            return

        # 2. Process all admin callbacks before registration check
        if data == 'admin' or data.startswith('admin_'):
            if not is_admin:
                self.answer_callback(cb_id, 'Неизвестное действие')
                return

            if data == 'admin':
                self.answer_callback(cb_id)
                self.edit_message(chat_id, message_id, self._admin_text(),
                                  self._admin_keyboard())
                return

            if data == 'admin_check':
                self.answer_callback(cb_id, 'Запускаю проверку всех…')
                threading.Thread(target=self._admin_check_all, daemon=True).start()
                return

            if data in ('admin_health_db', 'admin_hc_db'):
                t0 = time.perf_counter()
                try:
                    db.session.execute(db.text("SELECT 1"))
                    latency_ms = round((time.perf_counter() - t0) * 1000, 2)
                    users_cnt = User.query.count()
                    works_cnt = WorkItem.query.count()
                    text = (
                        "💾 <b>База данных (SQLite)</b>\n"
                        "Статус: OK ✅\n"
                        f"Задержка DB: <code>{latency_ms} мс</code>\n"
                        f"Пользователей: {users_cnt} | Работ в БД: {works_cnt}"
                    )
                except Exception as e:
                    text = (
                        "💾 <b>База данных (SQLite)</b>\n"
                        "Статус: ОШИБКА ❌\n"
                        f"Ошибка: <code>{html.escape(str(e))}</code>"
                    )
                self.answer_callback(cb_id, "БД проверена")
                self.send_message(chat_id, text)
                return

            if data in ('admin_health_sched', 'admin_hc_sched'):
                try:
                    from app.scheduler import jobs as jobs_mod
                    sched = getattr(jobs_mod, '_scheduler', None)
                    if sched and getattr(sched, 'running', False):
                        jobs = sched.get_jobs() if hasattr(sched, 'get_jobs') else []
                        lines = [
                            "⏰ <b>Планировщик (APScheduler)</b>",
                            "Статус: Работает ✅",
                            f"Активных задач: {len(jobs)}",
                        ]
                        if jobs:
                            lines.append("\nБлижайшие задачи:")
                            for j in jobs[:5]:
                                next_run = j.next_run_time.strftime('%H:%M:%S') if getattr(j, 'next_run_time', None) else '—'
                                lines.append(f"• <code>{html.escape(str(j.id))}</code> ({next_run})")
                        text = "\n".join(lines)
                    else:
                        text = (
                            "⏰ <b>Планировщик (APScheduler)</b>\n"
                            "Статус: Остановлен или не инициализирован ⚠️\n"
                            "Активных задач: 0"
                        )
                except Exception as e:
                    text = (
                        "⏰ <b>Планировщик (APScheduler)</b>\n"
                        "Статус: ОШИБКА ❌\n"
                        f"Ошибка: <code>{html.escape(str(e))}</code>"
                    )
                self.answer_callback(cb_id, "Планировщик проверен")
                self.send_message(chat_id, text)
                return

            if data in ('admin_health_eios', 'admin_hc_timeline', 'admin_hc_eios'):
                try:
                    from app.parser.auth import build_auth
                    auth = build_auth(self.app)
                    t0 = time.perf_counter()
                    alive = auth.is_session_alive()
                    latency_ms = round((time.perf_counter() - t0) * 1000, 2)
                    text = (
                        "🌐 <b>EIOS / Timeline Health Check</b>\n"
                        f"Статус шлюза Timeline: {'Доступен (сессия жива) ✅' if alive else 'Требует входа ⚠️'}\n"
                        f"Задержка ответа: <code>{latency_ms} мс</code>\n"
                        f"Шлюз: <code>{html.escape(auth.base_url)}</code>"
                    )
                except Exception as e:
                    text = (
                        "🌐 <b>EIOS / Timeline Health Check</b>\n"
                        "Статус: Ошибка проверки ❌\n"
                        f"Ошибка: <code>{html.escape(str(e))}</code>"
                    )
                self.answer_callback(cb_id, "EIOS проверен")
                self.send_message(chat_id, text)
                return

            if data in ('admin_health_tg', 'admin_hc_tg'):
                try:
                    t0 = time.perf_counter()
                    res = self._api('getMe', timeout=10)
                    latency_ms = round((time.perf_counter() - t0) * 1000, 2)
                    raw_bot_name = getattr(self, 'username', None)
                    if not raw_bot_name and isinstance(res, dict):
                        raw_bot_name = res.get('username') or res.get('result', {}).get('username')
                    bot_name = raw_bot_name or 'бот'
                    text = (
                        "✈️ <b>Telegram Bot API Health Check</b>\n"
                        "Статус: OK ✅\n"
                        f"Задержка API (ping): <code>{latency_ms} мс</code>\n"
                        f"Бот: @{html.escape(str(bot_name))}"
                    )
                except Exception as e:
                    text = (
                        "✈️ <b>Telegram Bot API Health Check</b>\n"
                        "Статус: ОШИБКА ❌\n"
                        f"Ошибка: <code>{html.escape(str(e))}</code>"
                    )
                self.answer_callback(cb_id, "Telegram API проверен")
                self.send_message(chat_id, text)
                return

            if data == 'admin_maint_prompt':
                self.answer_callback(cb_id)
                self.send_message(
                    chat_id,
                    "⚠️ <b>Включение режима технических работ</b>\n\n"
                    "При включении тех. работ:\n"
                    "• Все регулярные уведомления студентам будут приостановлены\n"
                    "• Пользователям будет показываться экран тех. работ со случайной шуткой\n"
                    "• Администратор сохраняет полный доступ для отладки\n\n"
                    "Для подтверждения отправьте команду:\n"
                    "<code>/maintenance_confirm</code>"
                )
                return

            if data == 'admin_maint_off':
                s = Setting.query.first()
                if not s:
                    s = Setting()
                    db.session.add(s)
                s.maintenance_mode = False
                db.session.commit()
                self.answer_callback(cb_id, "Тех. работы выключены ✅")
                self.send_message(
                    chat_id,
                    "✅ <b>Режим технических работ ВЫКЛЮЧЕН</b>.\n\n"
                    "Бот снова доступен для всех пользователей в обычном режиме."
                )
                if message_id:
                    self.edit_message(chat_id, message_id, self._admin_text(),
                                      self._admin_keyboard(maintenance_mode=False))
                return

            if data == 'admin_msg_pick':
                self.answer_callback(cb_id)
                from app.models import User
                users = User.query.filter_by(is_registered=True).order_by(
                    User.last_check_at.desc().nullslast(), User.id.desc()
                ).limit(8).all()
                if not users:
                    users = User.query.order_by(User.id.desc()).limit(8).all()

                buttons = []
                for u in users:
                    display = f"👤 {u.title}"
                    if u.timeline_login:
                        display += f" ({u.timeline_login})"
                    buttons.append([{'text': display[:36], 'callback_data': f'admin_msg_to:{u.chat_id}'}])
                buttons.append([{'text': '🔙 Назад в меню', 'callback_data': 'admin'}])

                text = (
                    "<b>✉️ Выберите получателя:</b>\n\n"
                    "Нажмите на пользователя из списка ниже, либо отправьте команду:\n"
                    "<code>/msg &lt;chat_id или логин&gt; &lt;текст&gt;</code>"
                )
                self.edit_message(chat_id, message_id, text, {'inline_keyboard': buttons})
                return

            if data.startswith('admin_msg_to:'):
                self.answer_callback(cb_id)
                from app.models import User
                target_cid = data.split(':', 1)[1]
                target_user = User.query.filter_by(chat_id=target_cid).first()
                name = target_user.title if target_user else f"chat_id {target_cid}"
                self._admin_state[str(chat_id)] = {
                    'action': 'msg_user',
                    'target_chat_id': str(target_cid),
                    'target_name': name,
                }
                text = (
                    f"✍️ <b>Отправка сообщения пользователю {html.escape(name)}</b>\n"
                    f"Chat ID: <code>{target_cid}</code>\n\n"
                    "Напишите текст сообщения в чат следующим сообщением.\n"
                    "<i>Поддерживается форматирование Telegram. Для отмены нажмите кнопку ниже или введите /cancel.</i>"
                )
                cancel_kb = {'inline_keyboard': [[{'text': '❌ Отмена', 'callback_data': 'admin_msg_cancel'}]]}
                self.edit_message(chat_id, message_id, text, cancel_kb)
                return

            if data == 'admin_broadcast_prompt':
                self.answer_callback(cb_id)
                self._admin_state[str(chat_id)] = {
                    'action': 'broadcast',
                }
                text = (
                    "📢 <b>Рассылка всем пользователям</b>\n\n"
                    "Напишите текст сообщения в чат следующим сообщением (или используйте команду <code>/broadcast текст</code>).\n\n"
                    "<i>Сообщение будет отправлено всем активным пользователям бота.</i>"
                )
                cancel_kb = {'inline_keyboard': [[{'text': '❌ Отмена', 'callback_data': 'admin_msg_cancel'}]]}
                self.edit_message(chat_id, message_id, text, cancel_kb)
                return

            if data == 'admin_msg_cancel':
                self.answer_callback(cb_id, "Отменено")
                self._admin_state.pop(str(chat_id), None)
                self.edit_message(chat_id, message_id, self._admin_text(), self._admin_keyboard())
                return

            self.answer_callback(cb_id, 'Неизвестное действие')
            return

        if data == 'menu':
            self.answer_callback(cb_id)
            self._menu(chat_id, user=user)
            return

        if data == 'noop':
            self.answer_callback(cb_id)
            return

        # Действия с расписанием доступны всегда!
        if data.startswith('sch|'):
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
                self.answer_callback(cb_id, 'Расписание обновлено 🔄')
            else:
                self.answer_callback(cb_id)

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

        # --- Гороскоп доступен всегда (даже до входа в ЭИОС) ---
        if data.startswith(('horo|', 'horo_view|')):
            parts = data.split('|')
            sign = parts[1]
            from app.bot.horoscope import generate_horoscope, format_horoscope_html
            h = generate_horoscope(sign, user=user)
            self.answer_callback(cb_id)
            self.edit_message(chat_id, message_id, format_horoscope_html(h),
                              self._horoscope_card_keyboard(sign, user=user))
            return

        if data == 'horo_menu':
            self.answer_callback(cb_id)
            text = "🔮 <b>Выбери знак зодиака на сегодня:</b>"
            self.edit_message(chat_id, message_id, text,
                              self._horoscope_selection_keyboard(prefix='horo_view', user=user))
            return

        if data.startswith('horo_save_digest|'):
            parts = data.split('|')
            from app.bot.horoscope import normalize_zodiac_sign, ZODIAC_SIGNS, generate_horoscope, format_horoscope_html
            sign = normalize_zodiac_sign(parts[1])
            if sign:
                user.zodiac_sign = sign
                user.horoscope_notify = True
                db.session.commit()
                sign_name = ZODIAC_SIGNS.get(sign, {}).get('name', sign)
                self.answer_callback(cb_id, f"Знак сохранён: {sign_name} ✅", show_alert=True)
                h = generate_horoscope(sign, user=user)
                self.edit_message(chat_id, message_id, format_horoscope_html(h),
                                  self._horoscope_card_keyboard(sign, user=user))
            else:
                self.answer_callback(cb_id, "Неизвестный знак")
            return

        if data == 'horo_pick_sign':
            self.answer_callback(cb_id)
            text = "🔮 <b>Выбери знак зодиака для утреннего дайджеста:</b>"
            self.edit_message(chat_id, message_id, text,
                              self._horoscope_selection_keyboard(prefix='horo_digest_set', user=user, back_to_profile=True))
            return

        if data.startswith('horo_digest_set|'):
            parts = data.split('|')
            from app.bot.horoscope import normalize_zodiac_sign, ZODIAC_SIGNS
            sign = normalize_zodiac_sign(parts[1])
            if sign:
                user.zodiac_sign = sign
                user.horoscope_notify = True
                db.session.commit()
                sign_name = ZODIAC_SIGNS.get(sign, {}).get('name', sign)
                self.answer_callback(cb_id, f"Гороскоп включён: {sign_name} ✅")
            else:
                self.answer_callback(cb_id)
            self.edit_message(chat_id, message_id, self._build_profile_text(user),
                              self._profile_keyboard(user))
            return

        if data == 'toggle_horoscope_notify':
            if getattr(user, 'horoscope_notify', False):
                user.horoscope_notify = False
                db.session.commit()
                self.answer_callback(cb_id, '🔮 Гороскоп в дайджесте: ВЫКЛ')
                self.edit_message(chat_id, message_id, self._build_profile_text(user),
                                  self._profile_keyboard(user))
            else:
                if getattr(user, 'zodiac_sign', None):
                    user.horoscope_notify = True
                    db.session.commit()
                    from app.bot.horoscope import ZODIAC_SIGNS
                    sname = ZODIAC_SIGNS.get(user.zodiac_sign, {}).get('name', user.zodiac_sign)
                    self.answer_callback(cb_id, f'🔮 Гороскоп в дайджесте: ВКЛ ({sname}) ✅')
                    self.edit_message(chat_id, message_id, self._build_profile_text(user),
                                      self._profile_keyboard(user))
                else:
                    self.answer_callback(cb_id, 'Выбери свой знак зодиака')
                    text = "🔮 <b>Выбери свой знак зодиака для утреннего дайджеста (08:00):</b>"
                    self.edit_message(chat_id, message_id, text,
                                      self._horoscope_selection_keyboard(prefix='horo_digest_set', user=user, back_to_profile=True))
            return

        if not user.is_registered:
            self.answer_callback(cb_id, 'Сначала /login')
            self._ask_login(user, chat_id)
            return

        if data.startswith(('wv|', 'work_view|')):
            parts = data.split('|')
            ext_id = parts[1]
            sort = parts[2] if len(parts) > 2 else 'new'
            page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 1
            text, keyboard = self._build_work_detail(user, ext_id, sort=sort, page=page)
            self.answer_callback(cb_id)
            self.edit_message(chat_id, message_id, text, keyboard)
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
        elif data == 'toggle_theme_femboy':
            user.theme_femboy = not getattr(user, 'theme_femboy', False)
            db.session.commit()
            alert = '🐾 Тема: ВКЛ :3' if user.theme_femboy else '🐾 Тема: ВЫКЛ'
            self.answer_callback(cb_id, alert)
            if message_id:
                self.edit_message(chat_id, message_id, self._build_profile_text(user),
                                  self._profile_keyboard(user))
            else:
                self.send_message(chat_id, self._build_profile_text(user),
                                  reply_markup=self._profile_keyboard(user))
            self.send_message(chat_id, "🐾 Меню обновлено! :3" if user.theme_femboy else "Меню обновлено",
                              reply_markup=self._main_keyboard(user))
        elif data == 'relogin':
            self.answer_callback(cb_id)
            self._ask_login(user, chat_id, intro=False)
        elif data == 'unlink':
            self.answer_callback(cb_id, 'Данные удалены')
            self._cmd_unlink(user, chat_id)
        elif data == 'check':
            self.answer_callback(cb_id, 'Проверяю…')
            self._run_check_async(user.id, chat_id)
        else:
            self.answer_callback(cb_id, 'Неизвестное действие')

    # ---------------------------------------------------------------- сообщения

    def _handle_message(self, msg: dict):
        if self.app is not None and not has_app_context():
            with self.app.app_context():
                return self._handle_message(msg)

        from app.models import Setting, db

        chat = msg.get('chat') or {}
        if chat.get('type') != 'private':
            return  # групповые чаты не обслуживаем
        chat_id = str(chat.get('id'))
        text = (msg.get('text') or '').strip()
        from_user = msg.get('from') or {}
        user = self._get_or_create_user(chat, from_user)
        low = text.lower()
        norm = self._norm(text)

        admin_chat_id = self.app.config.get('ADMIN_CHAT_ID') if self.app else None
        is_admin = bool(admin_chat_id) and str(chat_id) == str(admin_chat_id)

        # 1. Intercept in maintenance mode for non-admin users
        if self.is_maintenance_mode() and not is_admin:
            maint_joke = joke('maintenance', user=user) or "Идут технические работы. Скоро вернёмся!"
            self.send_message(
                chat_id,
                "🔧 <b>Технические работы</b>\n\n"
                "Бот временно недоступен в связи с проведением технических работ.\n\n"
                f"<i>{html.escape(maint_joke)}</i>"
            )
            return

        # 2. Confirmation of maintenance mode by admin
        if (low == '/maintenance_confirm' or low.startswith('/maintenance_confirm')) and is_admin:
            s = Setting.query.first()
            if not s:
                s = Setting()
                db.session.add(s)
            s.maintenance_mode = True
            db.session.commit()
            self.send_message(
                chat_id,
                "🛑 <b>Режим технических работ ВКЛЮЧЁН</b>.\n\n"
                "• Все фоновые уведомления студентам приостановлены\n"
                "• Пользовательские команды и кнопки перехватываются с шуткой\n"
                "• У вас сохраняется полный доступ для отладки\n\n"
                "Выключить можно в /admin кнопкой «Выключить тех. работы»."
            )
            return

        # --- секретная команда /femboy (R5) ---
        if low.startswith('/femboy') or norm in ('femboy', 'фембой'):
            user.femboy_unlocked = True
            db.session.commit()
            self.send_message(
                chat_id,
                "🐾 <b>Секретный режим разблокирован! :3</b>\n\n"
                "Переключатель темы теперь доступен в твоём <b>Профиле</b> (/profile).\n"
                "Мягких лапок и приятной учёбы! uwu",
                reply_markup=self._main_keyboard(user) if user.is_registered else None
            )
            return

        # --- команды, доступные всегда ---
        # --- обработка интерактивных действий администратора (ввод текста для юзера / рассылки) ---
        if is_admin and str(chat_id) in self._admin_state:
            state = self._admin_state[str(chat_id)]
            if low in ('/cancel', 'отмена') or norm == 'отмена':
                self._admin_state.pop(str(chat_id), None)
                self.send_message(chat_id, "❌ Отправка отменена.", reply_markup=self._admin_keyboard())
                return

            action = state.get('action')
            if action == 'msg_user':
                self._admin_state.pop(str(chat_id), None)
                target_cid = state['target_chat_id']
                target_name = state.get('target_name', target_cid)
                user_msg = f"📩 <b>Сообщение от администратора:</b>\n\n{text}"
                ok = self.send_message(target_cid, user_msg)
                if ok:
                    self.send_message(chat_id,
                                      f"✅ Сообщение успешно доставлено пользователю <b>{html.escape(target_name)}</b> (<code>{target_cid}</code>)!\n\n"
                                      f"<blockquote>{html.escape(text)}</blockquote>",
                                      reply_markup=self._admin_keyboard())
                else:
                    self.send_message(chat_id,
                                      f"⚠️ Не удалось доставить сообщение пользователю <b>{html.escape(target_name)}</b> (<code>{target_cid}</code>).\n"
                                      "Возможно, пользователь заблокировал бота.",
                                      reply_markup=self._admin_keyboard())
                return
            elif action == 'broadcast':
                self._admin_state.pop(str(chat_id), None)
                self.send_message(chat_id, "🚀 Запускаю рассылку…", reply_markup=self._admin_keyboard())
                threading.Thread(target=self._broadcast, args=(text,), daemon=True).start()
                return

        if low.startswith('/start') or norm == 'start':
            self._cmd_start(user, chat_id)
            return
        if low in ('/cancel', 'отмена') or norm == 'отмена':
            self._admin_state.pop(str(chat_id), None)
            user.state = 'ready' if user.is_registered else 'login'
            user.pending_login = None
            db.session.commit()
            self.send_message(chat_id, "Отменено.")
            return
        if low in ('/help', 'help', 'помощь', 'хелп') or norm in ('помощь', 'хелп') or text in (BTN_HELP, BTN_HELP_FEMBOY) or 'помощь' in low or 'хелп' in low:
            self.send_message(chat_id, self._help_text(user=user),
                              reply_markup=self._main_keyboard(user) if user.is_registered else None)
            return

        if low.startswith('/admin') and is_admin:
            self._admin_state.pop(str(chat_id), None)
            self.send_message(chat_id, self._admin_text(),
                              reply_markup=self._admin_keyboard())
            return
        if low.startswith('/broadcast') and is_admin:
            body = text[len('/broadcast'):].strip()
            if not body:
                self.send_message(chat_id, "Использование: <code>/broadcast текст</code>")
                return
            self._broadcast(body)
            return
        if (low.startswith('/msg') or low.startswith('/message')) and is_admin:
            cmd = '/msg' if low.startswith('/msg') else '/message'
            rest = text[len(cmd):].strip()
            parts = rest.split(maxsplit=1)
            if len(parts) < 2:
                self.send_message(chat_id,
                                  "Использование: <code>/msg &lt;chat_id|логин|id&gt; текст</code>\n\n"
                                  "Пример: <code>/msg 999333 Привет, обнови данные в /login</code>")
                return
            target_query, body = parts[0].strip(), parts[1].strip()
            self._send_admin_message_to_user(chat_id, target_query, body)
            return

        # --- Расписание доступно для любого пользователя ---
        if (low.startswith(('/schedule', '/rasp', '/расписание'))
                or norm in ('расписание', 'расписаньице', 'пары', 'парочки')
                or text in (BTN_SCHEDULE, BTN_SCHEDULE_FEMBOY)):
            self._cmd_schedule(user, chat_id, mode='today')
            return

        # --- Гороскоп доступен для любого пользователя ---
        if (low.startswith(('/horoscope', '/horo', '/гороскоп'))
                or norm in ('гороскоп', 'гороскопчик', 'гороскоп з', 'гороскоп :3')
                or text in (BTN_HOROSCOPE, BTN_HOROSCOPE_FEMBOY)):
            self._cmd_horoscope(user, chat_id)
            return

        # --- Детальный просмотр работы по ID (/task или /work) ---
        if low.startswith(('/task', '/work', '/задание', '/работа')):
            parts = text.split()
            if len(parts) > 1 and parts[1].strip():
                ext_id = parts[1].strip()
                card_text, kb = self._build_work_detail(user, ext_id)
                self.send_message(chat_id, card_text, reply_markup=kb)
            else:
                self.send_message(chat_id, "ℹ️ Использование: <code>/task 479796</code> (укажи ID задания)")
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
            self._menu(chat_id, user=user)
        elif low.startswith('/status') or norm in ('статус', 'статусик', 'статусик :3') or text in (BTN_STATUS, BTN_STATUS_FEMBOY):
            text, keyboard = self._build_status_page(user)
            self.send_message(chat_id, text, reply_markup=keyboard)
        elif low.startswith('/stats') or norm in ('статистика', 'статистика :3'):
            self._cmd_stats(user, chat_id)
        elif low.startswith('/today') or norm in ('на сегодня', 'сегодня', 'на сегодня :3') or text in (BTN_TODAY, BTN_TODAY_FEMBOY):
            self._cmd_today(user, chat_id)
        elif low.startswith('/find') or norm.startswith('поиск'):
            parts = text.split(maxsplit=1)
            query = parts[1].strip() if len(parts) > 1 else ''
            self._cmd_find(user, chat_id, query)
        elif low.startswith('/feedback') or norm.startswith('отзыв'):
            parts = text.split(maxsplit=1)
            msg_text = parts[1].strip() if len(parts) > 1 else ''
            self._cmd_feedback(user, chat_id, msg_text)
        elif (low.startswith('/overdue')
              or norm in ('просрочки', 'просроченные', 'хвостики-долги', 'хвостики', 'долги')
              or text in (BTN_OVERDUE, BTN_OVERDUE_FEMBOY)):
            self.send_message(chat_id, self._build_overdue_text(user))
        elif low.startswith('/check') or norm in ('проверить', 'потыкать лапкой') or text in (BTN_CHECK, BTN_CHECK_FEMBOY):
            self._run_check_async(user.id, chat_id)
        elif low.startswith('/profile') or norm in ('профиль', 'профиль з', 'профиль :з') or text in (BTN_PROFILE, BTN_PROFILE_FEMBOY):
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
                              + joke_html('unknown', user=user),
                              reply_markup=self._main_keyboard(user))

    # ----------------------------------------------------- проверки (в потоках)

    def _cmd_stats(self, user, chat_id: str):
        """Статистика работ пользователя."""
        from app.models import WorkItem
        from app.services import enrich_work_item as _enrich

        is_femboy = bool(getattr(user, 'theme_femboy', False))
        items = WorkItem.query.filter_by(user_id=user.id).all()
        if not items:
            self.send_message(chat_id, '<i>Заданьиц пока нет :3 Нажми «🐾 Потыкать лапкой».</i>' if is_femboy else '<i>Работ пока нет. Нажми «🔄 Проверить».</i>')
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

        if is_femboy:
            lines = [
                '<b>🐾 Статистика заданьиц :3</b>',
                '',
                f'Всего заданьиц: <b>{total}</b>',
                f'🎀 Сдано лапками: <b>{len(sent)}</b>',
                f'⏳ Ожидает отправки: <b>{len(pending)}</b>',
                f'😿 Просрочено (хвостики): <b>{len(overdue_list)}</b>',
                f'🚨 Горит (≤48 ч.): <b>{len(burning)}</b>',
            ]
            if nearest:
                countdown = _format_countdown(nearest.deadline)
                lines += [
                    '',
                    '⏰ Ближайший дедлайнчик:',
                    f'<b>{html.escape(nearest.discipline)}</b>',
                    f'Дедлайнчик: {nearest.deadline}  {countdown}',
                ]
            lines += ['', f'👥 Группка: <code>{html.escape(user.current_group)}</code> :3']
        else:
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

        is_femboy = bool(getattr(user, 'theme_femboy', False))
        if not urgent:
            no_urg_text = ('✅ <b>Горящих дедлайнчиков нет! :3</b>\n\n'
                           '<i>Заданьиц на сегодня и завтра нет — можно помурчать и отдохнуть uwu</i>\n\n'
                           if is_femboy else
                           '✅ <b>Горящих дедлайнов нет!</b>\n\n'
                           '<i>Работ на сегодня и завтра нет — можно выдохнуть.</i>\n\n')
            self.send_message(chat_id, no_urg_text + joke_html('overdue_clean', user=user))
            return

        hdr = f'<b>🐾 Горящие дедлайнчики сегодня и завтра ({len(urgent)}) :3:</b>' if is_femboy else f'<b>📋 Дедлайны сегодня и завтра ({len(urgent)}):</b>'
        lines = [hdr, '']
        for dt, it in urgent:
            countdown = _format_countdown(it.deadline)
            overdue = dt < now
            if is_femboy:
                icon = '😿' if overdue else ('🚨' if (dt - now).total_seconds() < 12 * 3600 else '🐾')
            else:
                icon = '💀' if overdue else ('🚨' if (dt - now).total_seconds() < 12 * 3600 else '🔴')
            lines.append(f'{icon} <b>{html.escape(it.discipline)}</b>')
            dl_lbl = 'Дедлайнчик:' if is_femboy else 'Срок:'
            lines.append(f'   {dl_lbl} {it.deadline}  {countdown}')
            lines.append('')

        self.send_message(chat_id, '\n'.join(lines))

    def _cmd_horoscope(self, user, chat_id: str):
        """Команда /horoscope: вывод гороскопа на сегодня или меню выбора знака."""
        zodiac_key = getattr(user, 'zodiac_sign', None)
        if zodiac_key:
            from app.bot.horoscope import generate_horoscope, format_horoscope_html
            h = generate_horoscope(zodiac_key, user=user)
            text = format_horoscope_html(h)
            kb = self._horoscope_card_keyboard(zodiac_key, user=user)
            self.send_message(chat_id, text, reply_markup=kb)
        else:
            text = "🔮 <b>Выбери свой знак зодиака на сегодня:</b>"
            kb = self._horoscope_selection_keyboard(prefix='horo_view', user=user)
            self.send_message(chat_id, text, reply_markup=kb)

    def _cmd_find(self, user, chat_id: str, query: str):
        """Поиск работы по названию дисциплины, теме или тексту задания."""
        from app.models import WorkItem
        from app.services import enrich_work_item as _enrich

        is_femboy = bool(getattr(user, 'theme_femboy', False))
        if not query:
            hint = ("ℹ️ Напиши поисковый запрос лапками :3 Например:\n<code>/find история</code> или <code>/find матем</code>"
                    if is_femboy else
                    "ℹ️ Напиши поисковый запрос, например:\n<code>/find история</code> или <code>/find матем</code>")
            self.send_message(chat_id, hint)
            return

        q_lower = query.lower()
        items = WorkItem.query.filter_by(user_id=user.id).all()
        matched = []
        for it in items:
            disc = (it.discipline or '').lower()
            pname = (getattr(it, 'point_name', '') or '').lower()
            ctrl = (getattr(it, 'control_text', '') or '').lower()
            if q_lower in disc or q_lower in pname or q_lower in ctrl:
                matched.append(it)

        if not matched:
            not_found = (f"😿 По запросу «<b>{html.escape(query)}</b>» лапки ничего не нашли :3"
                         if is_femboy else
                         f"🔍 По запросу «<b>{html.escape(query)}</b>» ничего не найдено.")
            self.send_message(chat_id, not_found)
            return

        hdr_find = f"🐾 <b>Найдено лапками ({len(matched)}) :3:</b>\n" if is_femboy else f"🔍 <b>Результаты поиска ({len(matched)}):</b>\n"
        lines = [hdr_find]
        action_row = []
        for idx, it in enumerate(matched[:10], start=1):
            e = _enrich(it)
            icon = _status_icon(e['status'], e['is_overdue'], is_femboy=is_femboy)
            countdown = _format_countdown(e['deadline']) if e.get('deadline') else ''
            lines.append(f"{icon} <b>{idx}. {html.escape(e['discipline'])}</b>")
            if e.get('point_name'):
                topic_lbl = "🐾 Тема заданьица:" if is_femboy else "📌 Тема:"
                lines.append(f"   {topic_lbl} {html.escape(e['point_name'])}")
            if e['deadline']:
                dl_lbl = "⏳ Дедлайнчик:" if is_femboy else "⏳ Срок:"
                lines.append(f"   {dl_lbl} {e['deadline']}" + (f"  ({countdown})" if countdown else ""))

            status_val = e['status']
            if is_femboy:
                if status_val in ('', 'Не отправлена'):
                    status_val = 'Не сдано лапками :3'
                elif 'отправлена' in status_val.lower():
                    status_val = 'Сдано лапками :3'
                elif any(k in status_val.lower() for k in ('зачтена', 'проверена', 'принята')):
                    status_val = 'Зачтено :3'

            if e.get('points'):
                pts_lbl = "🎯 Балльчики:" if is_femboy else "🎯 Баллы:"
                lines.append(f"   {pts_lbl} {html.escape(e['points'])} | Статус: {status_val}")
            else:
                lines.append(f"   Статус: {status_val}")
            lines.append("")

            ext_id = str(it.external_id or it.id)
            has_note = bool(getattr(it, 'teacher_note', None))
            btn_icon = ('🎀' if has_note else '🐾') if is_femboy else ('💬' if has_note else '📋')
            action_row.append({
                'text': f"{btn_icon} #{idx}",
                'callback_data': f"wv|{ext_id}|new|1",
            })

        keyboard = None
        if action_row:
            kb_rows = [action_row[i:i+4] for i in range(0, len(action_row), 4)]
            keyboard = {'inline_keyboard': kb_rows}

        self.send_message(chat_id, "\n".join(lines), reply_markup=keyboard)

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
        fb_kb = {'inline_keyboard': [[{'text': '✉️ Ответить пользователю', 'callback_data': f'admin_msg_to:{chat_id}'}]]}
        ok = self.send_message(admin_chat_id, admin_msg, reply_markup=fb_kb)
        if ok:
            self.send_message(chat_id, "✅ Твоё сообщение успешно отправлено администратору! Спасибо.")
        else:
            self.send_message(chat_id, "⚠️ Не удалось доставить сообщение администратору.")

    def _run_check_async(self, user_id: int, chat_id: str):
        """Ручная проверка: одно сообщение, которое потом правится результатом."""
        user_for_check = None
        if self.app is not None:
            with self.app.app_context():
                from app.models import User
                user_for_check = User.query.get(user_id) if hasattr(User, 'query') else None
        is_femboy = bool(user_for_check and getattr(user_for_check, 'theme_femboy', False))

        start_msg = ("🐾 Стучусь лапкой в Timeline… :3\n\n" + joke_html('checking', user=user_for_check)
                     if is_femboy else
                     "🔄 Захожу в Timeline…\n\n" + joke_html('checking', user=user_for_check))
        status_id = self.send_message_id(chat_id, start_msg)

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
                    diag = format_diagnostic_block('TimelineScraper', error, '3/3')
                    text = (f"⚠️ Ошибка проверки: <code>{html.escape(error)}</code>\n\n"
                            f"{diag}\n\n"
                            + joke_html('apology', user=user))
                    keyboard = None
                elif result == CHECK_SKIPPED:
                    text, keyboard = self._build_status_page(user)
                    skip_prefix = ("🕒 Только что тыкал лапкой — не тревожу сайт зря :3\n\n"
                                   if getattr(user, 'theme_femboy', False) else
                                   "🕒 Только что проверял — не долблю сайт зря.\n\n")
                    text = skip_prefix + text
                else:
                    text, keyboard = self._build_status_page(user)
                if not self.edit_message(chat_id, status_id, text, keyboard):
                    self.send_message(chat_id, text, reply_markup=keyboard)

        threading.Thread(target=worker, daemon=True).start()

    def _admin_text(self) -> str:
        if self.app is not None and not has_app_context():
            with self.app.app_context():
                return self._admin_text()
        from app.models import Setting, User, WorkItem
        total = User.query.count()
        registered = User.query.filter_by(is_registered=True).count()
        active = User.query.filter_by(is_registered=True, is_active=True).count()
        works = WorkItem.query.count()
        femboy_count = User.query.filter_by(theme_femboy=True).count()
        s = Setting.query.first()
        maint_str = "ВКЛЮЧЁН 🔴" if (s and getattr(s, 'maintenance_mode', False)) else "ВЫКЛЮЧЕН 🟢"
        return (
            "<b>🛠 Админ-сводка</b>\n"
            f"Режим тех. работ: <b>{maint_str}</b>\n"
            f"Всего чатов: {total}\n"
            f"С данными ЭИОС: {registered}\n"
            f"Активных: {active}\n"
            f"Работ в базе: {works}\n"
            f"Фембой-тема (активно): {femboy_count} 🐾\n\n"
            + joke_html('admin')
        )

    def _admin_check_all(self):
        from app.scheduler.jobs import check_all_users
        try:
            check_all_users(self.app, force=True)
        except Exception as e:
            logger.error(f"Проверка всех не удалась: {e}")

    def _broadcast(self, body: str):
        if self.app is not None and not has_app_context():
            with self.app.app_context():
                return self._broadcast(body)
        from app.models import User
        chat_ids = [u.chat_id for u in
                    User.query.filter_by(is_registered=True, is_active=True).all()]
        sent = 0
        for cid in chat_ids:
            try:
                if self.send_message(cid, body):
                    sent += 1
            except Exception as e:
                logger.warning(f"Broadcast failed for chat_id={cid}: {e}")
            time.sleep(0.01)
        admin = self.app.config.get('ADMIN_CHAT_ID') if self.app else None
        if admin:
            self.send_message(admin, f"Рассылка: доставлено {sent} из {len(chat_ids)}")

    def _send_admin_message_to_user(self, admin_chat_id: str, target_query: str, body: str) -> bool:
        """Отправка персонального сообщения пользователю от имени администратора."""
        if self.app is not None and not has_app_context():
            with self.app.app_context():
                return self._send_admin_message_to_user(admin_chat_id, target_query, body)
        from app.models import User
        target_user = User.query.filter_by(chat_id=str(target_query)).first()
        if not target_user:
            target_user = User.query.filter(User.timeline_login.ilike(target_query)).first()
        if not target_user and str(target_query).isdigit():
            target_user = User.query.get(int(target_query))

        if target_user:
            target_cid = str(target_user.chat_id)
            target_name = target_user.title
        elif str(target_query).isdigit():
            target_cid = str(target_query)
            target_name = f"chat_id {target_query}"
        else:
            self.send_message(admin_chat_id,
                              f"❌ Пользователь «<code>{html.escape(target_query)}</code>» не найден в базе данных.")
            return False

        user_msg = f"📩 <b>Сообщение от администратора:</b>\n\n{body}"
        ok = self.send_message(target_cid, user_msg)
        if ok:
            self.send_message(
                admin_chat_id,
                f"✅ Сообщение успешно доставлено пользователю <b>{html.escape(target_name)}</b> (<code>{target_cid}</code>)!\n\n"
                f"<blockquote>{html.escape(body)}</blockquote>"
            )
            return True
        else:
            self.send_message(
                admin_chat_id,
                f"⚠️ Не удалось доставить сообщение пользователю <b>{html.escape(target_name)}</b> (<code>{target_cid}</code>).\n"
                "Возможно, пользователь заблокировал бота."
            )
            return False

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

def notify_new_works(sender, chat_id, works: list, user: Any = None):
    if is_maintenance_mode(getattr(sender, 'app', None)):
        return
    if not works:
        return
    if user is None:
        try:
            from app.models import User
            user = User.query.filter_by(chat_id=str(chat_id)).first()
        except Exception:
            user = None
    lines = ["<b>Новые работы в Timeline:</b>\n"]
    for w in works:
        if w.get('is_overdue'):
            lines.append(f"💀 Просрочена работа <b>{html.escape(w['discipline'])}</b>")
            lines.append("  ⚠️ Срок сдачи уже прошёл!")
        else:
            lines.append(f"• <b>{html.escape(w['discipline'])}</b>")
        if w.get('point_name'):
            lines.append(f"  📌 Тема: {html.escape(w['point_name'])}")
        if w.get('deadline'):
            lines.append(f"  Срок: {w['deadline']}")
        detected_files = extract_file_attachments(f"{w.get('control_text', '')} {w.get('teacher_note', '')}")
        if detected_files:
            lines.append(f"  📁 Файл: " + ", ".join(f"<code>{html.escape(f)}</code>" for f in detected_files[:2]))
        if w.get('points'):
            lines.append(f"  🎯 Баллы: {html.escape(w['points'])}")
        lines.append(f"  Статус: {w.get('status') or 'Не отправлена'}")
        lines.append("")
    lines.append(joke_html('new_works', user=user))
    sender.send_message(chat_id, "\n".join(lines))


def notify_status_changes(sender, chat_id, changes: list, user: Any = None):
    if is_maintenance_mode(getattr(sender, 'app', None)):
        return
    if not changes:
        return
    if user is None:
        try:
            from app.models import User
            user = User.query.filter_by(chat_id=str(chat_id)).first()
        except Exception:
            user = None
    lines = ["<b>Изменения статуса работ:</b>\n"]
    for c in changes:
        if c.get('is_overdue'):
            lines.append(f"💀 Просрочена: <b>{html.escape(c['discipline'])}</b>")
        else:
            lines.append(f"• <b>{html.escape(c['discipline'])}</b>")
        if c.get('point_name'):
            lines.append(f"  📌 Тема: {html.escape(c['point_name'])}")
        if c.get('deadline'):
            lines.append(f"  Срок: {c['deadline']}")
        detected_files = extract_file_attachments(f"{c.get('control_text', '')} {c.get('teacher_note', '')}")
        if detected_files:
            lines.append(f"  📁 Файл: " + ", ".join(f"<code>{html.escape(f)}</code>" for f in detected_files[:2]))
        lines.append(f"  Статус: {c['old_status'] or 'Не отправлена'} → "
                     f"<b>{c['new_status'] or 'Не отправлена'}</b>")
        if c.get('points'):
            lines.append(f"  🎯 Баллы: {html.escape(c['points'])}")
        if c.get('teacher_note'):
            lines.append(f"  💬 <b>Рецензия преподавателя:</b>\n<blockquote>{html.escape(c['teacher_note'])}</blockquote>")
        lines.append("")
    lines.append(joke_html('status_change', user=user))
    sender.send_message(chat_id, "\n".join(lines))


def notify_deadline_reminders(sender, chat_id, groups: dict, user: Any = None):
    if is_maintenance_mode(getattr(sender, 'app', None)):
        return
    if user is None:
        try:
            from app.models import User
            user = User.query.filter_by(chat_id=str(chat_id)).first()
        except Exception:
            user = None
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
        lines.append(joke_html('reminder', user=user))
        sender.send_message(chat_id, "\n".join(lines))


def notify_overdue(sender, chat_id, works: list, user: Any = None):
    if is_maintenance_mode(getattr(sender, 'app', None)):
        return
    if not works:
        return
    if user is None:
        try:
            from app.models import User
            user = User.query.filter_by(chat_id=str(chat_id)).first()
        except Exception:
            user = None
    lines = ["<b>💀 ПРОСРОЧЕННЫЕ РАБОТЫ:</b>\n"]
    for w in works:
        lines.append(f"• <b>{html.escape(w['discipline'])}</b>")
        lines.append(f"  Дедлайн был: {w['deadline']}")
        lines.append("")
    lines.append(joke_html('overdue_alert', user=user))
    sender.send_message(chat_id, "\n".join(lines))


def notify_error(sender, chat_id, text: str, module: str = 'TimelineScraper',
                 description: str | None = None, attempts: str = '3/3',
                 tz_offset_hours: int = 3, user: Any = None):
    """Отправка уведомления об ошибке пользователю с моноширинным диагностическим блоком и извинением (R3)."""
    if is_maintenance_mode(getattr(sender, 'app', None)):
        return
    from app.services import format_diagnostic_block

    if user is None:
        try:
            from app.models import User
            user = User.query.filter_by(chat_id=str(chat_id)).first()
        except Exception:
            user = None

    desc = description or (text.split('\n')[0].replace('⚠️', '').strip() if text else "Ошибка синхронизации")
    diag_block = format_diagnostic_block(module, desc, attempts=attempts, tz_offset_hours=tz_offset_hours)
    apology = joke_html('apology', user=user) or joke_html('error', user=user)

    if '<code>Модуль:' in text:
        msg = f"{text}\n\n{apology}"
    else:
        msg = f"{text}\n\n{diag_block}\n\n{apology}"
    sender.send_message(chat_id, msg)


def notify_admin(sender, admin_chat_id, text: str):
    """Служебное сообщение админу (проблемы сайта/парсера)."""
    sender.send_message(admin_chat_id, f"🛠 {text}\n\n{joke_html('error')}")
