FROM debian:bookworm-slim

# Install system deps (python3-venv only — app runs from /venv, not system pip)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl unzip ca-certificates \
    sqlite3 \
    python3 python3-venv \
    cron \
    tzdata \
    bash findutils \
  && rm -rf /var/lib/apt/lists/* \
  && rm -rf \
    /usr/lib/python3/dist-packages/pip* \
    /usr/lib/python3/dist-packages/setuptools* \
    /usr/lib/python3/dist-packages/pkg_resources*

# Install rclone
RUN curl https://rclone.org/install.sh | bash

# Python venv — upgrade pip/setuptools to avoid known CVEs in bundled versions
RUN python3 -m venv /venv
ENV PATH="/venv/bin:$PATH"
RUN pip install --no-cache-dir --upgrade "pip>=26.1.2" "setuptools>=78.1.1" \
  && pip install --no-cache-dir flask python-crontab gunicorn

WORKDIR /app

# App files
COPY app/server.py         /app/server.py
COPY app/config_zip.py     /app/config_zip.py
COPY app/stats_history.py  /app/stats_history.py
COPY static/           /app/static/
COPY scripts/sync.sh /scripts/sync.sh
COPY scripts/sync-job.sh /scripts/sync-job.sh
RUN chmod +x /scripts/sync.sh /scripts/sync-job.sh

# Entrypoint
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Config & data volumes
VOLUME ["/config", "/data"]

ENV CONFIG_DIR=/config
ENV SOURCE_DIR=/data
ENV REMOTE_PATH=protondrive:NAS-Backup
ENV RCLONE_CONFIG=/config/rclone.conf

EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]
