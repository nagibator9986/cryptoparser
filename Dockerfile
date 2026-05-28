FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CRYPTO_MONITOR_DB_PATH=/data/crypto_monitor.sqlite3 \
    CRYPTO_MONITOR_ENV=production

WORKDIR /app

COPY pyproject.toml README.md ./
COPY crypto_monitor ./crypto_monitor
COPY crypto-monitor-skills ./crypto-monitor-skills
COPY config ./config
COPY scripts ./scripts

RUN pip install --no-cache-dir .

EXPOSE 8080
VOLUME ["/data"]

ENTRYPOINT ["crypto-monitor"]
CMD ["railway"]
