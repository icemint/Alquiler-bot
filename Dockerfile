FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=America/Argentina/Buenos_Aires \
    DATABASE_PATH=/data/listings.sqlite3

WORKDIR /app

# Dependencies first so code changes do not invalidate the wheel cache.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY monitor/ ./monitor/

# The SQLite file lives on a volume so it survives container replacement.
RUN useradd --create-home --uid 10001 monitor \
    && mkdir -p /data \
    && chown -R monitor:monitor /data /app
USER monitor

VOLUME ["/data"]

HEALTHCHECK --interval=5m --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import os,sys,sqlite3,time; p=os.getenv('DATABASE_PATH','/data/listings.sqlite3'); sys.exit(0 if os.path.exists(p) else 1)"

CMD ["python", "-m", "monitor"]
