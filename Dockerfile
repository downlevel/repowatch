FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    REPOWATCH_DB=/data/repowatch.db \
    REPOWATCH_BACKUP_DIR=/data/backups

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY repowatch/ ./repowatch/
RUN pip install --no-cache-dir . \
    && groupadd --gid 10001 repowatch \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /app repowatch \
    && mkdir /data \
    && chown 10001:10001 /data

USER 10001:10001
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD ["python", "-m", "repowatch.healthcheck"]
ENTRYPOINT ["repowatch"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8765"]
