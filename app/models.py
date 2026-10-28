from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def utc_now() -> datetime:
    """Naive UTC (без tzinfo) — так хранит SQLite и так сравнивают все вызовы."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Setting(db.Model):
    """Глобальные настройки бота (ровно одна строка). Управляются из админки."""
    id = db.Column(db.Integer, primary_key=True)
    telegram_bot_token = db.Column(db.String(200))
    check_interval = db.Column(db.Integer, default=300)  # дефолт для новых юзеров, сек
    last_check_at = db.Column(db.DateTime)

    # NEW v2.1: Глобальный режим технических работ (R4)
    maintenance_mode = db.Column(db.Boolean, default=False)


class User(db.Model):
    """Пользователь бота: свой логин/пароль ЭИОС и свой набор работ."""
    __tablename__ = 'bot_user'

    id = db.Column(db.Integer, primary_key=True)
    chat_id = db.Column(db.String(32), unique=True, nullable=False, index=True)
    username = db.Column(db.String(100))
    first_name = db.Column(db.String(100))

    timeline_login = db.Column(db.String(200))
    password_enc = db.Column(db.Text)

    is_registered = db.Column(db.Boolean, default=False)
    is_active = db.Column(db.Boolean, default=True)
    # login | password | validating | ready
    state = db.Column(db.String(20), default='login')
    pending_login = db.Column(db.String(200))

    notify_enabled = db.Column(db.Boolean, default=True)
    check_interval = db.Column(db.Integer, default=0)  # 0 = использовать глобальный

    # Академическая группа для расписания (по умолчанию МКТ-2531)
    group_name = db.Column(db.String(50), default='МКТ-2531')
    # Была ли группа установлена пользователем вручную (если False — автоопределяется из ЭИОС)
    group_manually_set = db.Column(db.Boolean, default=False)
    # Сессионные куки для повторного использования (ускорение проверок и защита от блокировок)
    session_cookies = db.Column(db.Text)

    # Настройки уведомлений расписания
    schedule_notify = db.Column(db.Boolean, default=False)  # слать расписание на завтра
    schedule_notify_time = db.Column(db.String(5), default='22:00')  # HH:MM

    # Утренний дайджест дедлайнов
    digest_notify = db.Column(db.Boolean, default=False)
    digest_notify_time = db.Column(db.String(5), default='08:00')

    # Для уведомлений об изменении расписания
    schedule_hash = db.Column(db.String(64))  # MD5 последнего расписания

    # Когда последний раз уходили дайджест/вечернее расписание (дедупликация в течение дня)
    digest_last_sent = db.Column(db.DateTime)
    schedule_last_sent = db.Column(db.DateTime)

    created_at = db.Column(db.DateTime, default=utc_now)
    last_check_at = db.Column(db.DateTime)
    last_error = db.Column(db.String(300))

    # NEW v2.1: Счётчик непрерывных неудач проверки (R2)
    fail_streak = db.Column(db.Integer, default=0)

    # NEW v2.1: Секретная фембой/фурри локализация (R5)
    femboy_unlocked = db.Column(db.Boolean, default=False)
    theme_femboy = db.Column(db.Boolean, default=False)

    # NEW v3.0: Ежедневный студенческий гороскоп
    horoscope_notify = db.Column(db.Boolean, default=False)
    zodiac_sign = db.Column(db.String(30))

    works = db.relationship('WorkItem', backref='user', lazy='select',
                            cascade='all, delete-orphan')

    @property
    def title(self) -> str:
        if self.username:
            return f"@{self.username}"
        return self.first_name or f"id{self.chat_id}"

    @property
    def interval(self) -> int:
        return self.check_interval or 0

    @property
    def current_group(self) -> str:
        return self.group_name or 'МКТ-2531'


class WorkItem(db.Model):
    """Работа (дисциплина) конкретного пользователя."""
    __tablename__ = 'user_work_item'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer,
                        db.ForeignKey('bot_user.id', ondelete='CASCADE'),
                        nullable=False, index=True)
    external_id = db.Column(db.String(50), nullable=False)
    discipline = db.Column(db.String(300), nullable=False)
    deadline = db.Column(db.String(100))
    status = db.Column(db.String(100))
    first_seen_at = db.Column(db.DateTime, default=utc_now)
    last_updated_at = db.Column(db.DateTime, default=utc_now,
                                onupdate=utc_now)
    notified_new = db.Column(db.Boolean, default=False)

    reminder_48_sent = db.Column(db.Boolean, default=False)
    reminder_24_sent = db.Column(db.Boolean, default=False)
    reminder_12_sent = db.Column(db.Boolean, default=False)
    overdue_sent = db.Column(db.Boolean, default=False)

    # NEW v2.1: Явный признак просроченности работы (R1)
    is_overdue = db.Column(db.Boolean, default=False)

    # NEW v3.0: Детальные поля задания из SendGrid
    point_name = db.Column(db.String(300))  # тема / контрольная точка
    control_text = db.Column(db.Text)       # текст задания преподавателя
    points = db.Column(db.String(50))       # текущие/макс баллы, напр. "5 из 5"
    teacher_note = db.Column(db.Text)       # рецензия / комментарий преподавателя
    unavailability_reason = db.Column(db.String(255))  # инфо о доступности задания

    __table_args__ = (
        db.UniqueConstraint('user_id', 'external_id', name='uq_user_work'),
    )
