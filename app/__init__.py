import logging

from flask import Flask

from app.config import Config
from app.models import db


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
        db.create_all()  # схему меняем только через пересоздание instance/timeline.db
        from app.models import Setting
        if not Setting.query.first():
            db.session.add(Setting(check_interval=app.config['DEFAULT_CHECK_INTERVAL']))
            db.session.commit()

    return app
