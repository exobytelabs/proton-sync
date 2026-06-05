#!/bin/bash
set -e

# Load overrides.env when paths are not locked by docker-compose
OVERRIDES=/config/overrides.env
if [ -f "$OVERRIDES" ] && [ "${PROTON_SYNC_PATHS_LOCKED:-}" != "1" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$OVERRIDES"
  set +a
fi

# Ensure directories exist
mkdir -p /config/logs /config/db

# Restore config from backup.zip when local config is missing
if [ -f /config/backup.zip ]; then
  python3 /app/config_zip.py import /config/backup.zip --if-empty || true
fi

# Apply timezone for cron schedules and log timestamps
if [ -n "${TZ:-}" ] && [ -f "/usr/share/zoneinfo/${TZ}" ]; then
  ln -sf "/usr/share/zoneinfo/${TZ}" /etc/localtime
  echo "${TZ}" > /etc/timezone
fi

# Start cron daemon (for scheduled syncs)
service cron start

echo "=== ProtonSync starting ==="
echo "  Timezone: ${TZ:-UTC}"
echo "  Source:  ${SOURCE_DIR:-/data}"
echo "  Remote:  ${REMOTE_PATH:-protondrive:NAS-Backup}"
echo "  Config backup: download zip from UI or place backup.zip in /config"
echo "  Web UI:  http://0.0.0.0:8080"
if [ -n "${UI_PASSWORD:-}" ]; then
  echo "  UI Auth: enabled (user: ${UI_USERNAME:-admin})"
else
  echo "  UI Auth: disabled (set UI_PASSWORD to enable)"
fi
echo "=========================="

exec gunicorn \
  --bind 0.0.0.0:8080 \
  --workers 1 \
  --timeout 300 \
  --access-logfile - \
  --error-logfile - \
  "server:app"
