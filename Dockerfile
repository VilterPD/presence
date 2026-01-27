FROM python:3.12-slim

# unbuffered logs (nice for docker logs)
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# minimal system deps (optional: sqlite3 CLI only if you want to debug inside container)
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
    ca-certificates \
    sqlite3 \
 && rm -rf /var/lib/apt/lists/*

# install python deps first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# copy app
COPY . .

# make entrypoint executable
RUN chmod +x /app/entrypoint.sh \
 # create non-root user
 && useradd -r -u 10001 presence \
 # ensure data dir exists and is writable
 && mkdir -p /data \
 && chown -R presence:presence /app /data

# default DB path in container (compose can override)
ENV DB_PATH=/data/presence.db

# persistent data
VOLUME ["/data"]

# drop privileges
USER presence

ENTRYPOINT ["./entrypoint.sh"]
