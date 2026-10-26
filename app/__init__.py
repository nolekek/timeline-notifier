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
            # 1. bot_user: настройки, расписание, дайджесты, стрик неудач и темы
            res_user = conn.execute(text("PRAGMA table_info(bot_user)")).fetchall()
            if res_user:
                existing_user = {row[1] for row in res_user}
                user_columns = [
                    ('group_name', "VARCHAR(50) DEFAULT 'МКТ-2531'"),
                    ('group_manually_set', "BOOLEAN DEFAULT 0"),
                    ('session_cookies', "TEXT"),
                    ('schedule_notify', "BOOLEAN DEFAULT 0"),
                    ('schedule_notify_time', "VARCHAR(5) DEFAULT '22:00'"),
                    ('digest_notify', "BOOLEAN DEFAULT 0"),
                    ('digest_notify_time', "VARCHAR(5) DEFAULT '08:00'"),
                    ('schedule_hash', "VARCHAR(64)"),
                    ('digest_last_sent', "DATETIME"),
                    ('schedule_last_sent', "DATETIME"),
                    ('fail_streak', "INTEGER DEFAULT 0"),
                    ('femboy_unlocked', "BOOLEAN DEFAULT 0"),
                    ('theme_femboy', "BOOLEAN DEFAULT 0"),
                ]
                for col_name, col_def in user_columns:
                    if col_name not in existing_user:
                        conn.execute(text(f"ALTER TABLE bot_user ADD COLUMN {col_name} {col_def}"))

            # 2. setting: глобальный режим тех. работ
            res_setting = conn.execute(text("PRAGMA table_info(setting)")).fetchall()
            if res_setting:
                existing_setting = {row[1] for row in res_setting}
                setting_columns = [
                    ('maintenance_mode', "BOOLEAN DEFAULT 0"),
                ]
                for col_name, col_def in setting_columns:
                    if col_name not in existing_setting:
                        conn.execute(text(f"ALTER TABLE setting ADD COLUMN {col_name} {col_def}"))

            # 3. user_work_item: флаг просроченности работы
            res_work = conn.execute(text("PRAGMA table_info(user_work_item)")).fetchall()
            if res_work:
                existing_work = {row[1] for row in res_work}
                work_columns = [
                    ('is_overdue', "BOOLEAN DEFAULT 0"),
                ]
                for col_name, col_def in work_columns:
                    if col_name not in existing_work:
                        conn.execute(text(f"ALTER TABLE user_work_item ADD COLUMN {col_name} {col_def}"))

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
