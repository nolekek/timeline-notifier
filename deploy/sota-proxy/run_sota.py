#!/usr/bin/env python3
"""
SotaProxy Runner — автоматический мост SotaVPN (VLESS Reality) -> SOCKS5 для Telegram Bot.
Отреверснутый протокол SotaConnect (InterHive / KittenCore / Sing-box).
"""
import json
import logging
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("sota-proxy")

CONFIG_PATH = "/etc/sing-box/config.json"
CACHE_PATH = "/app/last_working_config.json"
HWID_FILE = "/app/hwid.txt"

API_DOMAINS = [
    "https://meow5g.com",
    "https://ltekitten.com",
    "https://sotacdn.com",
]


def get_or_create_hwid() -> str:
    if os.path.exists(HWID_FILE):
        try:
            with open(HWID_FILE, "r", encoding="utf-8") as f:
                h = f.read().strip()
                if h:
                    return h
        except Exception:
            pass
    new_hwid = str(uuid.uuid4()).upper()
    try:
        with open(HWID_FILE, "w", encoding="utf-8") as f:
            f.write(new_hwid)
    except Exception:
        pass
    return new_hwid


def fetch_sota_outbound(access_key: str, gate_id: int, hwid: str) -> dict | None:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    headers = {
        "User-Agent": "Dart/3.5 (dart:io)",
        "X-Access-Key": access_key.strip(),
        "X-HwID": hwid,
        "Accept": "application/json",
    }

    last_error = None
    for domain in API_DOMAINS:
        url = f"{domain}/api/v1/public/connection/connect?gate_id={gate_id}"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
                if resp.getcode() == 200:
                    raw = resp.read()
                    data = json.loads(raw.decode("utf-8"))
                    cfg = data.get("configuration", {})
                    outbounds = cfg.get("outbounds", [])
                    if outbounds:
                        logger.info(f"Успешно получена конфигурация ноды с {domain}")
                        return outbounds
        except Exception as e:
            last_error = e
            continue

    logger.warning(f"Не удалось получить конфигурацию ноды из API SotaVPN: {last_error}")
    return None


def generate_singbox_config(outbounds: list, socks_port: int) -> dict:
    # Удаляем устаревшие / неподдерживаемые типы outbounds (type: "dns" удален в sing-box 1.13+)
    valid_outbounds = [o for o in outbounds if o.get("type") != "dns"]

    for o in valid_outbounds:
        if "multiplex" in o and not o["multiplex"]:
            del o["multiplex"]
        if "transport" in o and not o["transport"]:
            del o["transport"]

    # Гарантируем наличие direct outbound
    if not any(o.get("tag") == "direct" for o in valid_outbounds):
        valid_outbounds.append({"type": "direct", "tag": "direct"})

    # Гарантируем наличие block outbound
    if not any(o.get("tag") == "block" for o in valid_outbounds):
        valid_outbounds.append({"type": "block", "tag": "block"})

    return {
        "log": {
            "level": "info",
            "timestamp": True,
        },
        "inbounds": [
            {
                "type": "socks",
                "tag": "socks-in",
                "listen": "0.0.0.0",
                "listen_port": socks_port,
            }
        ],
        "outbounds": valid_outbounds,
        "route": {
            "rules": [
                {
                    "domain_suffix": [
                        "rsvpu.ru",
                        "eios.rsvpu.ru",
                        "timeline.rsvpu.ru",
                    ],
                    "outbound": "direct",
                },
                {
                    "domain_suffix": [
                        "telegram.org",
                        "t.me",
                        "telegra.ph",
                        "telegram.dog",
                    ],
                    "outbound": "proxy",
                },
                {
                    "outbound": "proxy",
                },
            ],
            "auto_detect_interface": True,
        },
    }


def main():
    access_key = os.getenv("SOTAVPN_KEY", "").strip()
    gate_id = int(os.getenv("SOTA_GATE_ID", "8"))
    socks_port = int(os.getenv("SOCKS_PORT", "1080"))

    if not access_key:
        logger.warning("=======================================================")
        logger.warning("SOTAVPN_KEY не задан в .env!")
        logger.warning("Укажи SOTAVPN_KEY=твой-ключ-uuid в файле .env")
        logger.warning("sota-proxy переходит в режим ожидания (idle)")
        logger.warning("=======================================================")
        while True:
            time.sleep(3600)

    hwid = get_or_create_hwid()
    logger.info(f"Запуск sota-proxy (HWID: {hwid}, gate_id: {gate_id}, port: {socks_port})")

    outbounds = None
    for attempt in range(5):
        outbounds = fetch_sota_outbound(access_key, gate_id, hwid)
        if outbounds:
            break
        logger.info(f"Повтор запроса к API SotaVPN через 3 сек (попытка {attempt+1}/5)...")
        time.sleep(3)

    if not outbounds and os.path.exists(CACHE_PATH):
        logger.warning("Используем сохраненную резервную конфигурацию ноды...")
        try:
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                outbounds = json.load(f)
        except Exception as e:
            logger.error(f"Ошибка чтения кэша: {e}")

    if not outbounds:
        logger.error("КРИТИЧЕСКАЯ ОШИБКА: Не удалось получить ноды SotaVPN ни из API, ни из кэша!")
        sys.exit(1)

    # Фильтруем устаревшие outbounds
    outbounds = [o for o in outbounds if o.get("type") != "dns"]

    # Сохраняем в кэш
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(outbounds, f, indent=2)
    except Exception:
        pass

    # Генерируем конфиг sing-box
    sb_config = generate_singbox_config(outbounds, socks_port)
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(sb_config, f, indent=2, ensure_ascii=False)

    logger.info(f"Конфигурация sing-box записана в {CONFIG_PATH}")
    logger.info(f"Запуск sing-box на 0.0.0.0:{socks_port} (SOCKS5)...")

    # Запускаем sing-box
    sing_box_bin = "/usr/local/bin/sing-box"
    if not os.path.exists(sing_box_bin):
        sing_box_bin = "sing-box"

    cmd = [sing_box_bin, "run", "-c", CONFIG_PATH]
    sys.stdout.flush()
    sys.stderr.flush()
    os.execvp(sing_box_bin, cmd)


if __name__ == "__main__":
    main()
