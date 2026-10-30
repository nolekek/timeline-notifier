# SotaProxy Dockerfile — Sing-box + VLESS Reality клиент для SotaVPN
FROM ghcr.io/sagernet/sing-box:latest AS sing-box-bin

FROM python:3.12-alpine

RUN apk add --no-cache ca-certificates tzdata

COPY --from=sing-box-bin /usr/local/bin/sing-box /usr/local/bin/sing-box

WORKDIR /app
COPY run_sota.py /app/run_sota.py

ENV PYTHONUNBUFFERED=1 \
    SOCKS_PORT=1080 \
    SOTA_GATE_ID=8

EXPOSE 1080

ENTRYPOINT ["python", "/app/run_sota.py"]
