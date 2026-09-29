"""Шифрование паролей ЭИОС перед записью в БД.

Если установлен `cryptography` — используется Fernet (AES128-CBC + HMAC).
Если нет — обратимое обфусцирование XOR+base64 (лучше, чем ничего, но не крипта).
Ключ берётся из CRED_KEY (или SECRET_KEY). Смена ключа = потеря паролей.
"""
import base64
import hashlib
import logging
import os

logger = logging.getLogger(__name__)

_FERNET_PREFIX = 'enc:v1:'
_XOR_PREFIX = 'obf:v1:'


def _key_material() -> bytes:
    return (os.getenv('CRED_KEY') or os.getenv('SECRET_KEY')
            or 'dev-secret-change-me').encode('utf-8')


def _key() -> bytes:
    return hashlib.sha256(_key_material()).digest()


try:
    from cryptography.fernet import Fernet

    _fernet = Fernet(base64.urlsafe_b64encode(_key()))
    HAS_FERNET = True
except Exception:  # pragma: no cover - зависит от окружения
    _fernet = None
    HAS_FERNET = False


def _xor(data: bytes) -> bytes:
    k = _key()
    return bytes(b ^ k[i % len(k)] for i, b in enumerate(data))


def encrypt(plain: str) -> str:
    if not plain:
        return ''
    raw = plain.encode('utf-8')
    if _fernet is not None:
        return _FERNET_PREFIX + _fernet.encrypt(raw).decode('ascii')
    logger.warning('cryptography не установлен — пароль сохраняется в обфусцированном виде')
    return _XOR_PREFIX + base64.b64encode(_xor(raw)).decode('ascii')


def decrypt(stored: str) -> str:
    if not stored:
        return ''
    try:
        if stored.startswith(_FERNET_PREFIX):
            if _fernet is None:
                logger.error('Пароль зашифрован Fernet, но cryptography не установлен')
                return ''
            return _fernet.decrypt(stored[len(_FERNET_PREFIX):].encode('ascii')).decode('utf-8')
        if stored.startswith(_XOR_PREFIX):
            return _xor(base64.b64decode(stored[len(_XOR_PREFIX):])).decode('utf-8')
    except Exception as e:
        logger.error(f'Не удалось расшифровать пароль: {e}')
        return ''
    return stored  # legacy: пароль лежал открытым текстом
