"""Healthcheck для Docker: приложение и БД живы?

Бьём в лёгкий /ping (проверка SELECT 1), а не в главную страницу — та рендерит
всю таблицу пользователей. 200 = живой, 401 = включён ADMIN_PASSWORD (для /ping
не бывает, но на всякий случай тоже «живой»).
"""
import os
import sys
import urllib.error
import urllib.request

port = os.getenv('WEB_PORT', '5000')
url = f'http://127.0.0.1:{port}/ping'

try:
    with urllib.request.urlopen(url, timeout=5) as resp:
        code = resp.status
except urllib.error.HTTPError as e:
    code = e.code  # 401 при Basic Auth — нормально
except Exception as e:  # noqa: BLE001 - любой сбой сети/сокета = unhealthy
    print(f'healthcheck: {e}', file=sys.stderr)
    sys.exit(1)

print(f'healthcheck: HTTP {code}')
sys.exit(0 if code < 500 else 1)
