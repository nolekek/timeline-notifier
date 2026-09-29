from app import create_app
from app.bot.telegram_bot import start_bot
from app.scheduler.jobs import start_scheduler

app = create_app()

if __name__ == '__main__':
    start_scheduler(app)
    start_bot(app)
    print(f">>> Админка: http://{app.config['WEB_HOST']}:{app.config['WEB_PORT']}/")
    print(">>> Бот: напиши ему /start")
    app.run(
        host=app.config['WEB_HOST'],
        port=app.config['WEB_PORT'],
        debug=False,
        use_reloader=False,
        threaded=True,
    )
