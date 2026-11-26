FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SAVEPOINT_DATA_DIR=/data \
    XDG_CACHE_HOME=/tmp/cache \
    PUID=1000 \
    PGID=1000

# rclone for the optional offsite backup; tzdata so TZ works.
RUN apt-get update \
    && apt-get install -y --no-install-recommends rclone tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8080
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/v1/health', timeout=4).status == 200 else 1)"

ENTRYPOINT ["/entrypoint.sh"]
# One worker on purpose: SQLite, the scheduler and per-game locks live in-process.
CMD ["uvicorn", "app.asgi:app", "--host", "0.0.0.0", "--port", "8080", \
     "--proxy-headers", "--forwarded-allow-ips", "*", \
     "--timeout-keep-alive", "75", "--no-server-header"]
