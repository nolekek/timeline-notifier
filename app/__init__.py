import logging

from flask import Flask
from sqlalchemy import event
from sqlalchemy.engine import Engine

from app.config import Config
from app.models import db


@event.listens_for(Engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    """Оптимизация SQLite: WAL-режим, внешние ключи и увеличенный busy_timeout."""
    if type(dbapi_connection).__module__.startswith("sqlite3"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute("PRAGMA busy_timeout = 5000")
        cursor.close()


def _auto_migrate():
    """Безопасная автомиграция: добавляет недостающие колонки в существующие таблицы."""
    try:
        from sqlalchemy import text
        with db.engine.connect() as conn:
            res = conn.execute(text("PRAGMA table_info(bot_user)")).fetchall()
            if res:
                existing_cols = {row[1] for row in res}
                if 'group_name' not in existing_cols:
                    conn.execute(text("ALTER TABLE bot_user ADD COLUMN group_name VARCHAR(50) DEFAULT 'МКТ-2531'"))
                if 'session_cookies' not in existing_cols:
                    conn.execute(text("ALTER TABLE bot_user ADD COLUMN session_cookies TEXT"))
                if 'schedule_notify' not in existing_cols:
                    conn.execute(text("ALTER TABLE bot_user ADD COLUMN schedule_notify BOOLEAN DEFAULT 0"))
                if 'schedule_notify_time' not in existing_cols:
                    conn.execute(text("ALTER TABLE bot_user ADD COLUMN schedule_notify_time VARCHAR(5) DEFAULT '22:00'"))
                if 'digest_notify' not in existing_cols:
                    conn.execute(text("ALTER TABLE bot_user ADD COLUMN digest_notify BOOLEAN DEFAULT 0"))
                if 'digest_notify_time' not in existing_cols:
                    conn.execute(text("ALTER TABLE bot_user ADD COLUMN digest_notify_time VARCHAR(5) DEFAULT '08:00'"))
                if 'schedule_hash' not in existing_cols:
                    conn.execute(text("ALTER TABLE bot_user ADD COLUMN schedule_hash VARCHAR(64)"))
                if 'digest_last_sent' not in existing_cols:
                    conn.execute(text("ALTER TABLE bot_user ADD COLUMN digest_last_sent DATETIME"))
                if 'schedule_last_sent' not in existing_cols:
                    conn.execute(text("ALTER TABLE bot_user ADD COLUMN schedule_last_sent DATETIME"))
                conn.commit()
    except Exception as e:
        logging.getLogger(__name__).warning(f"Ошибка автомиграции: {e}")


def create_app():
    """Собирает Flask-приложение: конфиг, БД, роуты админки."""
    app = Flask(__name__)
    app.config.from_object(Config)

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%H:%M:%S',
    )

    db.init_app(app)

    from app.routes.web import web_bp
    app.register_blueprint(web_bp)

    with app.app_context():
        db.create_all()
        _auto_migrate()
        from app.models import Setting
        if not Setting.query.first():
            db.session.add(Setting(check_interval=app.config['DEFAULT_CHECK_INTERVAL']))
            db.session.commit()

    import os
    if app.config.get('SECRET_KEY') in ('', 'dev-secret-change-me'):
        # Локальная отладка без .env — можно, но явно
        if os.getenv('FLASK_DEBUG') == '1':
            app.config['SECRET_KEY'] = 'dev-secret-change-me'
        else:
            raise RuntimeError(
                'SECRET_KEY не задан. Сгенерируй ключ '
                '(python -c "import secrets;print(secrets.token_hex(32))") '
                'и впиши его в .env — без него подписывать сессии админки нечем.'
            )
    if not app.config.get('ADMIN_PASSWORD') and not app.debug:
        import warnings
        warnings.warn(
            'ADMIN_PASSWORD не задан — веб-панель открыта без пароля! '
            'Задай ADMIN_PASSWORD в .env для защиты в production.',
            RuntimeWarning, stacklevel=2,
        )

    return app
