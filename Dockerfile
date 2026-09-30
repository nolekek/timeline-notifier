# Timeline Notifier — образ приложения: бот + планировщик + админка в одном процессе.
#
#   docker compose up -d --build     (см. docker-compose.yml)
#
# База и debug-дампы лежат НЕ в образе, а в ./instance (монтируется томом), поэтому
# пересборка образа их не трогает.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Moscow

# tzdata нужен, чтобы datetime.now() считал московское время (напоминания 48/24/12ч),
# ca-certificates — чтобы ходить в Timeline и Telegram по https.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# сначала зависимости — так слой кешируется и пересборка после правок кода быстрая
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# запуск не от root; uid фиксирован (10001), чтобы на сервере можно было
# сделать chown -R 10001:10001 ./instance
RUN useradd --create-home --uid 10001 app \
 && mkdir -p /app/instance \
 && chown -R app:app /app
USER app

EXPOSE 5000

# живой ли процесс: / отдаёт 200 (или 401, если включён ADMIN_PASSWORD)
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "tools/healthcheck.py"]

CMD ["python", "run.py"]
