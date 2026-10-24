import os

from app import create_app
from app.bot.telegram_bot import start_bot
from app.scheduler.jobs import start_scheduler

app = create_app()

if __name__ == '__main__':
    host = app.config.get('WEB_HOST', '0.0.0.0')
    port = int(app.config.get('WEB_PORT', 5000))

    start_scheduler(app)
    start_bot(app)
    print(f">>> Админка: http://{host}:{port}/")
    print(">>> Бот: напиши ему /start")

    use_waitress = (
        os.getenv('USE_WAITRESS', '').lower() in ('1', 'true', 'yes')
        or os.getenv('FLASK_ENV', '') == 'production'
    )

    if use_waitress:
        try:
            from waitress import serve
            import logging
            logging.getLogger('waitress').setLevel(logging.INFO)
            print(f'Starting Waitress on {host}:{port}')
            serve(app, host=host, port=port, threads=4)
        except ImportError:
            print('Waitress не установлен (pip install waitress). Запускаю Werkzeug.')
            app.run(host=host, port=port, use_reloader=False, threaded=True)
    else:
        app.run(
            host=host,
            port=port,
            debug=os.getenv('FLASK_DEBUG', '0') == '1',
            use_reloader=False,
            threaded=True,
        )
