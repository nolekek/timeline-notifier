import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
INSTANCE_DIR = BASE_DIR / 'instance'


def _ensure_instance_dir(path: Path) -> None:
    """Папка instance/ должна существовать и быть доступной на запись.

    В докере самая частая причина «unable to open database file» — том ./instance,
    владелец которого не совпадает с пользователем внутри контейнера (uid 10001).
    Поэтому падаем сразу и с понятным текстом, а не посреди работы.
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / '.write-test'
        probe.write_text('ok', encoding='utf-8')
        probe.unlink()
    except OSError as e:
        raise RuntimeError(
            f'Нет доступа на запись в {path} ({e}).\n'
            'Если это Docker: контейнер должен работать под тем же uid/gid, что владелец\n'
            'папки instance (в .env задай APP_UID/APP_GID, проверить: id -u и id -g).\n'
            'Либо, если оставляешь встроенного пользователя 10001: '
            'sudo chown -R 10001:10001 ./instance'
        ) from e


_ensure_instance_dir(INSTANCE_DIR)


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value not in (None, '') else default


class Config:
    SECRET_KEY = os.getenv('SECRET_KEY', 'dev-secret-change-me')

    SQLALCHEMY_DATABASE_URI = _env(
        'DATABASE_URL', f"sqlite:///{INSTANCE_DIR / 'timeline.db'}")
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # --- Timeline / ЭИОС ---
    TIMELINE_BASE_URL = _env('TIMELINE_BASE_URL', 'https://timeline.rsvpu.ru')
    TIMELINE_LOGIN_URL = _env('TIMELINE_LOGIN_URL', 'https://eios.rsvpu.ru/Account/Login')
    TIMELINE_WORKS_URL = _env('TIMELINE_WORKS_URL', 'https://timeline.rsvpu.ru/StudentWork/Send')
    TIMELINE_GRID_URL = _env('TIMELINE_GRID_URL', 'https://timeline.rsvpu.ru/StudentWork/SendGrid')
    # Проверка сертификатов УрГПУ: по умолчанию ВКЛЮЧЕНА (сертификаты у сайтов валидные).
    # Логин/пароль студентов ходят по этому соединению — отключать только осознанно.
    TIMELINE_VERIFY_SSL = _env('TIMELINE_VERIFY_SSL', 'true').lower() in ('1', 'true', 'yes')
    # Сохранять сырые страницы/ответы SendGrid в instance/debug — для починки парсера
    TIMELINE_DEBUG_DUMP = _env('TIMELINE_DEBUG_DUMP', 'false').lower() in ('1', 'true', 'yes')
    TIMELINE_DEBUG_DIR = str(INSTANCE_DIR / 'debug')

    # --- Telegram ---
    # Если api.telegram.org недоступен с сервера (РФ) — см. README:
    # TELEGRAM_API_BASE=https://<свой-мирор-домен>
    # TELEGRAM_PROXY=socks5h://127.0.0.1:1080 (tg-ws-proxy) или http://user:pass@host:port
    TELEGRAM_API_BASE = _env('TELEGRAM_API_BASE', 'https://api.telegram.org').rstrip('/')
    TELEGRAM_PROXY = os.getenv('TELEGRAM_PROXY', '').strip() or None
    TELEGRAM_TIMEOUT = int(_env('TELEGRAM_TIMEOUT', '20'))
    TELEGRAM_POLL_TIMEOUT = int(_env('TELEGRAM_POLL_TIMEOUT', '25'))

    # Админ бота (кто может /admin, /broadcast). Пусто = админ-команд нет.
    ADMIN_CHAT_ID = os.getenv('ADMIN_CHAT_ID', '').strip()

    # --- Проверки ---
    DEFAULT_CHECK_INTERVAL = int(_env('DEFAULT_CHECK_INTERVAL', '300'))  # сек, дефолт для юзера
    SCHEDULER_TICK = int(_env('SCHEDULER_TICK', '60'))  # как часто планировщик смотрит, кому пора
    CHECK_WORKERS = int(_env('CHECK_WORKERS', '4'))  # одновременных логинов в Timeline
    # Не логиниться в Timeline чаще, чем раз в N секунд на одного человека
    MIN_CHECK_GAP = int(_env('MIN_CHECK_GAP', '60'))
    # Повторы входа при обрывах связи (сайт УрГПУ иногда рвёт соединение)
    LOGIN_ATTEMPTS = int(_env('LOGIN_ATTEMPTS', '3'))
    LOGIN_RETRY_DELAY = int(_env('LOGIN_RETRY_DELAY', '2'))  # сек между попытками

    # --- Админка (веб) ---
    ADMIN_USER = os.getenv('ADMIN_USER', '').strip()
    ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', '').strip()
    WEB_HOST = _env('WEB_HOST', '0.0.0.0')
    WEB_PORT = int(_env('WEB_PORT', '5000'))
