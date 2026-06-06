#!/bin/bash
# Sync a single source → remote path pair

SOURCE_DIR="${SOURCE_DIR:?SOURCE_DIR required}"
REMOTE_PATH="${REMOTE_PATH:?REMOTE_PATH required}"
JOB_ID="${JOB_ID:-default}"
JOB_NAME="${JOB_NAME:-$JOB_ID}"
DB_DIR="${DB_DIR:-/config/db}/${JOB_ID}"
LOG_FILE="${LOG_FILE:-/config/logs/sync.log}"
LAST_RUN_FILE="${LAST_RUN_FILE:-/config/db/${JOB_ID}/last_run.json}"

mkdir -p "$DB_DIR"
mkdir -p "$(dirname "$LOG_FILE")"
mkdir -p "$(dirname "$LAST_RUN_FILE")"

DB_FILE="$DB_DIR/file_tracker.db"
FOLDER_DB_FILE="$DB_DIR/folder_tracker.db"
STDERR_FILE="$DB_DIR/rclone_stderr.txt"

RCLONE_LIVE_LOG="$DB_DIR/rclone_live.log"
RCLONE_STREAM_PID=""
RCLONE_LOG_ARGS=(--log-format "date,time" --log-level INFO --stats 5s)
# Proton Drive is rate-limited and can hang with default rclone settings — use conservative transfers + timeouts.
RCLONE_DRIVE_ARGS=(
  --transfers 2
  --checkers 4
  --retries 5
  --retries-sleep 10s
  --timeout 10m
  --contimeout 60s
  --low-level-retries 20
  --protondrive-replace-existing-draft=true
  --protondrive-enable-caching=false
)

# Mirror sync.log lines to stderr so `docker logs` matches the web UI Logs view.
append_log_line() {
    local line="$1"
    echo "$line" >> "$LOG_FILE"
    echo "$line" >&2
}

append_log_raw() {
    append_log_line "$1"
}

log_separator() {
    append_log_raw "$(printf '#%.0s' {1..80})"
}

stream_rclone_log() {
    local src="$1"
    : > "$src"
    (
        tail -n 0 -f "$src" 2>/dev/null | while IFS= read -r line || [ -n "$line" ]; do
            echo "$line" | grep -q "A file or folder with that name already exists (Code=2500, Status=422)" && continue
            append_log_line "$(date '+%Y-%m-%d %H:%M:%S') - [$JOB_NAME] $line"
        done
    ) &
    RCLONE_STREAM_PID=$!
}

stop_rclone_log_stream() {
    if [ -n "${RCLONE_STREAM_PID:-}" ]; then
        kill "$RCLONE_STREAM_PID" 2>/dev/null || true
        wait "$RCLONE_STREAM_PID" 2>/dev/null || true
        RCLONE_STREAM_PID=""
    fi
    sleep 0.2
}

run_rclone_logged() {
    local live_log="$1"
    shift
    stream_rclone_log "$live_log"
    if "$@" --log-file="$live_log" "${RCLONE_LOG_ARGS[@]}" 2>"$STDERR_FILE"; then
        stop_rclone_log_stream
        return 0
    fi
    stop_rclone_log_stream
    return 1
}

write_log() {
    if [ -f "$1" ]; then
        while IFS= read -r line; do
            if ! echo "$line" | grep -q "A file or folder with that name already exists (Code=2500, Status=422)"; then
                append_log_line "$(date '+%Y-%m-%d %H:%M:%S') - [$JOB_NAME] $line"
            fi
        done < "$1"
    else
        append_log_line "$(date '+%Y-%m-%d %H:%M:%S') - [$JOB_NAME] $1"
    fi
}

write_log_lines() {
    local file="$1"
    if [ -f "$file" ] && [ -s "$file" ]; then
        while IFS= read -r line; do
            [ -n "$line" ] && write_log "$line"
        done < "$file"
    fi
}

write_log_stderr() {
    local file="$1"
    local logged=0
    if [ ! -f "$file" ] || [ ! -s "$file" ]; then
        return
    fi
    while IFS= read -r line; do
        if echo "$line" | grep -qE '^Error:|Fatal error|ERROR :|error listing|error:'; then
            write_log "$line"
            logged=1
        fi
    done < "$file"
    if [ "$logged" -eq 0 ]; then
        grep -vE '^$|^Flags |^      --|^  -[a-zA-Z]|^Use "rclone|^Use "rclone help' "$file" \
            | tail -3 | while IFS= read -r line; do
            [ -n "$line" ] && write_log "$line"
        done
    fi
}

write_rclone_error() {
    local summary="$1"
    local temp_log="$2"
    local stderr_file="$3"
    write_log "ERROR: $summary"
    write_log_lines "$temp_log"
    write_log_stderr "$stderr_file"
}

rclone_error_summary() {
    local stderr_file="$1"
    local fallback="$2"
    if [ -f "$stderr_file" ] && [ -s "$stderr_file" ]; then
        grep -m1 'Fatal error\|ERROR\|error:' "$stderr_file" 2>/dev/null \
            | sed 's/^[0-9/]* [0-9:]* NOTICE: //' \
            | head -c 200
        return
    fi
    echo "$fallback"
}

append_log_raw ""
log_separator
write_log "Sync started | source=$SOURCE_DIR | remote=$REMOTE_PATH"
log_separator

log_footer() {
    log_separator
    write_log "Sync completed | new/changed=$NEW_CHANGED_COUNT | deleted=$DELETED_COUNT | deleted_folders=$DELETED_FOLDER_COUNT"
    log_separator
    append_log_raw ""
}

write_last_run_error() {
    local msg="${1:-sync failed}"
    local changed="${NEW_CHANGED_COUNT:-0}"
    local deleted="${DELETED_COUNT:-0}"
    local safe_msg
    safe_msg=$(printf '%s' "$msg" | sed 's/\\/\\\\/g; s/"/\\"/g')
    cat > "$LAST_RUN_FILE" <<EOF
{
  "job_id": "$JOB_ID",
  "job_name": "$JOB_NAME",
  "timestamp": "$(date -Iseconds)",
  "new_changed": $changed,
  "deleted": $deleted,
  "deleted_folders": 0,
  "status": "error",
  "error": "$safe_msg"
}
EOF
}

write_last_run_success() {
    cat > "$LAST_RUN_FILE" <<EOF
{
  "job_id": "$JOB_ID",
  "job_name": "$JOB_NAME",
  "timestamp": "$(date -Iseconds)",
  "new_changed": $NEW_CHANGED_COUNT,
  "deleted": $DELETED_COUNT,
  "deleted_folders": $DELETED_FOLDER_COUNT,
  "status": "success"
}
EOF
}

# ── File tracking DB ──────────────────────────────────────────────────────────
if [ ! -f "$DB_FILE" ]; then
    sqlite3 "$DB_FILE" <<SQL
CREATE TABLE files (path TEXT PRIMARY KEY, mtime INTEGER, size INTEGER);
CREATE TABLE metadata (key TEXT PRIMARY KEY, value INTEGER);
INSERT INTO metadata (key, value) VALUES ('last_run_time', 0);
SQL
fi

write_log "Scanning source directory..."
find "$SOURCE_DIR" -type f -printf "%p|%T@|%s\n" > "$DB_DIR/current_files.txt"
FILE_COUNT=$(wc -l < "$DB_DIR/current_files.txt" | tr -d ' ')
write_log "Scan complete: $FILE_COUNT files indexed"
CURRENT_TIMESTAMP=$(date +%s)

RELATIVE_SQL_CHANGED="SELECT ltrim(substr(fn.path, length('$SOURCE_DIR') + 1), '/') AS path FROM files_new fn LEFT JOIN files f ON fn.path = f.path WHERE f.path IS NULL OR fn.mtime > COALESCE(f.mtime, 0);"
RELATIVE_SQL_DELETED="SELECT ltrim(substr(f.path, length('$SOURCE_DIR') + 1), '/') AS path FROM files f WHERE f.path NOT IN (SELECT path FROM files_new);"

sqlite3 "$DB_FILE" <<SQL > /dev/null 2>&1
PRAGMA synchronous=OFF;
PRAGMA journal_mode=WAL;
DROP TABLE IF EXISTS files_new;
CREATE TABLE files_new (path TEXT PRIMARY KEY, mtime INTEGER, size INTEGER);
.import $DB_DIR/current_files.txt files_new
.output $DB_DIR/changed_files.txt
$RELATIVE_SQL_CHANGED
.output $DB_DIR/deleted_files.txt
$RELATIVE_SQL_DELETED
SQL

# ── Ensure remote destination exists ─────────────────────────────────────────
write_log "Checking remote destination..."
if ! rclone lsd "$REMOTE_PATH" >/dev/null 2>&1; then
    write_log "Creating remote folder: $REMOTE_PATH"
    if ! rclone mkdir "$REMOTE_PATH" 2>"$STDERR_FILE"; then
        write_rclone_error "could not create remote folder $REMOTE_PATH" "" "$STDERR_FILE"
        write_last_run_error "$(rclone_error_summary "$STDERR_FILE" "could not create remote folder")"
        rm -f "$STDERR_FILE"
        exit 1
    fi
    write_log "Remote folder created: $REMOTE_PATH"
else
    write_log "Remote folder exists: $REMOTE_PATH"
fi

NEW_CHANGED_COUNT=$(wc -l < "$DB_DIR/changed_files.txt" 2>/dev/null | tr -d ' ')
DELETED_COUNT=$(wc -l < "$DB_DIR/deleted_files.txt" 2>/dev/null | tr -d ' ')
write_log "Diff complete: $NEW_CHANGED_COUNT to upload, $DELETED_COUNT to delete"

# ── Upload changed/new files ──────────────────────────────────────────────────
if [ -s "$DB_DIR/changed_files.txt" ]; then
    write_log "Uploading $NEW_CHANGED_COUNT file(s) to $REMOTE_PATH (rclone stats every 5s, 10m per-file timeout)..."
    if ! run_rclone_logged "$RCLONE_LIVE_LOG" rclone sync "$SOURCE_DIR" "$REMOTE_PATH" \
        --files-from "$DB_DIR/changed_files.txt" \
        --update --local-no-check-updated \
        "${RCLONE_DRIVE_ARGS[@]}"; then
        write_rclone_error "rclone sync failed" "$RCLONE_LIVE_LOG" "$STDERR_FILE"
        write_last_run_error "$(rclone_error_summary "$STDERR_FILE" "rclone sync failed")"
        rm -f "$STDERR_FILE"
        exit 1
    fi
    write_log "Upload phase complete"
    rm -f "$RCLONE_LIVE_LOG" "$STDERR_FILE"
fi

# ── Delete removed files ──────────────────────────────────────────────────────
if [ -s "$DB_DIR/deleted_files.txt" ]; then
    write_log "Deleting $DELETED_COUNT remote file(s)..."
    if ! run_rclone_logged "$RCLONE_LIVE_LOG" rclone delete "$REMOTE_PATH" \
        --include-from "$DB_DIR/deleted_files.txt" \
        "${RCLONE_DRIVE_ARGS[@]}"; then
        write_rclone_error "rclone delete failed" "$RCLONE_LIVE_LOG" "$STDERR_FILE"
        write_last_run_error "$(rclone_error_summary "$STDERR_FILE" "rclone delete failed")"
        rm -f "$STDERR_FILE"
        exit 1
    fi
    write_log "Delete phase complete"
    rm -f "$RCLONE_LIVE_LOG" "$STDERR_FILE"
fi

# ── Commit file snapshot only after successful upload/delete ─────────────────
sqlite3 "$DB_FILE" <<SQL > /dev/null 2>&1
PRAGMA synchronous=OFF;
PRAGMA journal_mode=WAL;
BEGIN TRANSACTION;
DROP TABLE IF EXISTS files;
CREATE TABLE files AS SELECT * FROM files_new;
DROP TABLE files_new;
INSERT OR REPLACE INTO metadata (key, value) VALUES ('last_run_time', $CURRENT_TIMESTAMP);
COMMIT;
SQL

# ── Folder tracking ───────────────────────────────────────────────────────────
DELETED_FOLDERS_FILE="$DB_DIR/deleted_folders.txt"
CURRENT_FOLDERS_FILE="$DB_DIR/current_folders.txt"

FIRST_RUN=0
if [ ! -f "$FOLDER_DB_FILE" ]; then
    sqlite3 "$FOLDER_DB_FILE" <<SQL
CREATE TABLE folders (path TEXT PRIMARY KEY);
CREATE TABLE metadata (key TEXT PRIMARY KEY, value INTEGER);
INSERT INTO metadata (key, value) VALUES ('last_run_time', 0);
SQL
    FIRST_RUN=1
    write_log "Folder tracking: first run, building baseline (no deletions this run)"
fi

awk -F'|' '{sub(/[^/]*$/, "", $1); print $1}' "$DB_DIR/current_files.txt" | sort -u > "$CURRENT_FOLDERS_FILE"

RELATIVE_SQL_DELETED_FOLDERS="SELECT ltrim(substr(f.path, length('$SOURCE_DIR') + 1), '/') AS path FROM folders f WHERE f.path NOT IN (SELECT path FROM folders_new) ORDER BY path DESC;"

sqlite3 "$FOLDER_DB_FILE" <<SQL > /dev/null 2>&1
PRAGMA synchronous=OFF;
PRAGMA journal_mode=WAL;
DROP TABLE IF EXISTS folders_new;
CREATE TABLE folders_new (path TEXT PRIMARY KEY);
.import --csv $CURRENT_FOLDERS_FILE folders_new
$( [ "$FIRST_RUN" -eq 0 ] && echo ".output $DELETED_FOLDERS_FILE" )
$( [ "$FIRST_RUN" -eq 0 ] && echo "$RELATIVE_SQL_DELETED_FOLDERS" )
SQL

if [ "$FIRST_RUN" -eq 0 ] && [ -s "$DELETED_FOLDERS_FILE" ]; then
    FOLDER_DELETE_COUNT=$(wc -l < "$DELETED_FOLDERS_FILE" | tr -d ' ')
    write_log "Removing $FOLDER_DELETE_COUNT empty remote folder(s)..."
    while IFS= read -r folder; do
        write_log "Removing folder: $folder"
        if ! run_rclone_logged "$RCLONE_LIVE_LOG" rclone rmdir "$REMOTE_PATH/$folder"; then
            write_log "WARN: could not remove folder $folder (may not be empty)"
        fi
        rm -f "$RCLONE_LIVE_LOG"
    done < "$DELETED_FOLDERS_FILE"
fi

sqlite3 "$FOLDER_DB_FILE" <<SQL > /dev/null 2>&1
PRAGMA synchronous=OFF;
PRAGMA journal_mode=WAL;
BEGIN TRANSACTION;
DROP TABLE IF EXISTS folders;
CREATE TABLE folders AS SELECT * FROM folders_new;
DROP TABLE folders_new;
INSERT OR REPLACE INTO metadata (key, value) VALUES ('last_run_time', $CURRENT_TIMESTAMP);
COMMIT;
SQL

DELETED_FOLDER_COUNT=0
if [ "$FIRST_RUN" -eq 0 ]; then
    DELETED_FOLDER_COUNT=$(wc -l < "$DELETED_FOLDERS_FILE" 2>/dev/null || echo 0)
fi

rm -f "$DB_DIR/changed_files.txt" "$DB_DIR/deleted_files.txt" \
      "$DB_DIR/current_files.txt" "$DELETED_FOLDERS_FILE" "$CURRENT_FOLDERS_FILE"

log_footer
write_last_run_success
python3 /app/stats_history.py record \
    --config-dir "${CONFIG_DIR:-/config}" \
    --job-id "$JOB_ID" \
    --job-name "$JOB_NAME" \
    --new-changed "$NEW_CHANGED_COUNT" \
    --deleted "$DELETED_COUNT" \
    --deleted-folders "$DELETED_FOLDER_COUNT" \
    --db-dir "$DB_DIR" 2>/dev/null || true
exit 0
