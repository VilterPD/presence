FROM python:3.12-slim

# unbuffered logs (nice for docker logs)
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# install python deps first (layer caching)
COPY requirements.txt .
RUN umask 022 \
 && pip install --no-cache-dir -r requirements.txt \
 && chmod -R a+rX /usr/local/lib/python3.12/site-packages

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

# fail the build if the application user cannot import its dependencies
RUN python -c "import httpx, mohawk"

ENTRYPOINT ["./entrypoint.sh"]
