FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY crypto_monitor ./crypto_monitor
COPY crypto-monitor-skills ./crypto-monitor-skills
COPY config ./config
COPY scripts ./scripts

RUN pip install --no-cache-dir .

ENV CRYPTO_MONITOR_DB_PATH=/data/crypto_monitor.sqlite3
VOLUME ["/data"]

ENTRYPOINT ["crypto-monitor"]
CMD ["--help"]
