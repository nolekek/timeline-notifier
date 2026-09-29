import logging
import time
import urllib3
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logger = logging.getLogger(__name__)

# Причины неудачного входа: от них зависит, что говорить пользователю.
REASON_REJECTED = 'rejected'          # сайт явно не принял логин/пароль
REASON_NETWORK = 'network'            # таймаут, обрыв соединения, 5xx — проблема связи
REASON_SITE_CHANGED = 'site_changed'  # не нашли форму входа — поменялся сайт
REASON_FLOW = 'flow'                  # POST прошёл, но рабочая страница не открылась

REASON_TEXT = {
    REASON_REJECTED: 'сайт не принял логин/пароль',
    REASON_NETWORK: 'Timeline не отвечает (обрыв связи/таймаут)',
    REASON_SITE_CHANGED: 'на сайте изменилась страница входа (нужен фикс парсера)',
    REASON_FLOW: 'вход прошёл, но рабочая страница не открылась',
}


def _dump(debug_dir: str, text: str, note: str) -> str | None:
    """Сохранить страницу на диск — для разбора поломок парсера."""
    if not debug_dir:
        return None
    try:
        Path(debug_dir).mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        path = Path(debug_dir) / f'{note}-{stamp}.html'
        path.write_text(text, encoding='utf-8', errors='replace')
        logger.info(f'Дамп страницы сохранён: {path}')
        return str(path)
    except OSError as e:
        logger.warning(f'Не удалось сохранить дамп: {e}')
        return None


class TimelineAuth:
    def __init__(self,
                 base_url: str = 'https://timeline.rsvpu.ru',
                 login_url: str = 'https://eios.rsvpu.ru/Account/Login',
                 verify_ssl: bool = False,
                 debug_dir: str = None,
                 attempts: int = 3,
                 retry_delay: int = 2):
        self.base_url = base_url.rstrip('/')
        self.login_url = login_url
        self.verify_ssl = verify_ssl
        self.debug_dir = debug_dir
        self.attempts = max(1, attempts)
        self.retry_delay = max(0, retry_delay)
        self.reason = None  # см. REASON_* — заполняется после неудачного login()
        self.session = requests.Session()
        self.session.verify = verify_ssl
        self.session.headers.update({
            'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                           'AppleWebKit/537.36 (KHTML, like Gecko) '
                           'Chrome/120.0.0.0 Safari/537.36'),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
        })

    @staticmethod
    def _origin(url: str) -> str:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}"

    # ------------------------------------------------------------------ вход

    def login(self, username: str, password: str) -> bool:
        """Вход с повторами на транзиентных ошибках.

        Возвращает True/False, а в self.reason кладёт причину неудачи (REASON_*).
        Ретраим только сетевые сбои: если сайт явно отклонил пароль или сломался
        парсер — повторять бессмысленно.
        """
        for attempt in range(1, self.attempts + 1):
            ok, reason = self._login_once(username, password)
            if ok:
                self.reason = None
                return True
            self.reason = reason
            if reason in (REASON_REJECTED, REASON_SITE_CHANGED):
                return False
            if attempt < self.attempts:
                logger.warning(
                    f"Вход не удался ({REASON_TEXT.get(reason, reason)}), "
                    f"попытка {attempt}/{self.attempts} — повторяю через {self.retry_delay} с")
                time.sleep(self.retry_delay)
        logger.error(f"Вход не удался после {self.attempts} попыток: "
                     f"{REASON_TEXT.get(self.reason, self.reason)}")
        return False

    def _login_once(self, username: str, password: str) -> tuple[bool, str]:
        try:
            # 1. GET по всей OAuth-цепочке редиректов.
            #    resp.url — URL страницы, на которой реально оказалась форма логина
            #    (в нашем случае это timeline.rsvpu.ru/Account/OAuthLogin?...)
            resp = self.session.get(
                self.login_url,
                timeout=15,
                verify=self.verify_ssl,
                allow_redirects=True,
            )
            resp.raise_for_status()
            form_page_url = resp.url
            logger.info(f"Форма логина: {form_page_url}")
            _dump(self.debug_dir, resp.text, 'login-form')

            soup = BeautifulSoup(resp.text, 'html.parser')
            form = soup.find('form')
            if not form:
                logger.error("Форма логина не найдена на странице")
                _dump(self.debug_dir, resp.text, 'login-form-missing')
                return False, REASON_SITE_CHANGED

            # 2. Правильный URL для POST: склеиваем action с URL текущей страницы
            action = form.get('action') or ''
            post_url = urljoin(form_page_url, action)
            logger.info(f"POST -> {post_url}")

            # 3. Собираем payload из полей формы
            payload = {}
            for inp in form.find_all('input'):
                name = inp.get('name')
                if not name:
                    continue
                input_type = (inp.get('type') or 'text').lower()
                if input_type == 'hidden':
                    payload[name] = inp.get('value', '')
                elif input_type == 'password':
                    payload[name] = password
                elif input_type in ('text', 'email'):
                    payload[name] = username

            # 4. POST с обязательными Referer и Origin
            headers = {
                'Referer': form_page_url,
                'Origin': self._origin(form_page_url),
                'Content-Type': 'application/x-www-form-urlencoded',
            }
            login_resp = self.session.post(
                post_url,
                data=payload,
                headers=headers,
                timeout=15,
                allow_redirects=True,
                verify=self.verify_ssl,
            )
            login_resp.raise_for_status()
            logger.info(f"После POST: {login_resp.url}")
            _dump(self.debug_dir, login_resp.text, 'login-after-post')

            # Если сайт вернул нас обратно на форму — логин/пароль не приняты
            if 'Account/Login' in login_resp.url:
                logger.warning("Сайт вернул форму логина — логин/пароль не приняты")
                return False, REASON_REJECTED

            # 5. Проверка успеха — заходим на защищённую страницу
            check = self.session.get(
                f"{self.base_url}/StudentWork/Send",
                timeout=15,
                verify=self.verify_ssl,
                allow_redirects=True,
            )
            check.raise_for_status()
            if check.status_code == 200 and 'Account/Login' not in check.url:
                logger.info("Авторизация прошла успешно")
                return True, None

            logger.warning(f"Авторизация не подтверждена. URL: {check.url}")
            _dump(self.debug_dir, check.text, 'login-check-failed')
            return False, REASON_FLOW

        except requests.RequestException as e:
            # обрыв связи, таймаут, 5xx — это НЕ повод говорить «пароль неверный»
            logger.error(f"Ошибка при логине (связь): {e}")
            return False, REASON_NETWORK

    def get_session(self) -> requests.Session:
        return self.session


def build_auth(app) -> TimelineAuth:
    """TimelineAuth с адресами и настройками из конфига приложения."""
    return TimelineAuth(
        base_url=app.config['TIMELINE_BASE_URL'],
        login_url=app.config['TIMELINE_LOGIN_URL'],
        verify_ssl=app.config['TIMELINE_VERIFY_SSL'],
        debug_dir=debug_dir(app),
        attempts=app.config.get('LOGIN_ATTEMPTS', 3),
        retry_delay=app.config.get('LOGIN_RETRY_DELAY', 2),
    )


def debug_dir(app) -> str | None:
    """Куда складывать дампы страниц (или None, если дампы выключены)."""
    if app.config.get('TIMELINE_DEBUG_DUMP'):
        return app.config['TIMELINE_DEBUG_DIR']
    return None


def try_login(app, login: str, password: str) -> tuple[bool, str | None]:
    """Проверка пары логин/пароль. Возвращает (успех, причина неудачи)."""
    if not login or not password:
        return False, REASON_REJECTED
    auth = build_auth(app)
    ok = auth.login(login, password)
    return ok, auth.reason


def validate_login(app, login: str, password: str) -> bool:
    """Совместимая обёртка: только факт успеха."""
    return try_login(app, login, password)[0]
