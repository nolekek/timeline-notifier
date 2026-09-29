from datetime import datetime

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class Setting(db.Model):
    """Глобальные настройки бота (ровно одна строка). Управляются из админки."""
    id = db.Column(db.Integer, primary_key=True)
    telegram_bot_token = db.Column(db.String(200))
    check_interval = db.Column(db.Integer, default=300)  # дефолт для новых юзеров, сек
    last_check_at = db.Column(db.DateTime)


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

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_check_at = db.Column(db.DateTime)
    last_error = db.Column(db.String(300))

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
    first_seen_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                                onupdate=datetime.utcnow)
    notified_new = db.Column(db.Boolean, default=False)

    reminder_48_sent = db.Column(db.Boolean, default=False)
    reminder_24_sent = db.Column(db.Boolean, default=False)
    reminder_12_sent = db.Column(db.Boolean, default=False)
    overdue_sent = db.Column(db.Boolean, default=False)

    __table_args__ = (
        db.UniqueConstraint('user_id', 'external_id', name='uq_user_work'),
    )
