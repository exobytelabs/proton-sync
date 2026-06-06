#!/bin/bash
# Run all enabled backup jobs (or a single job when JOB_ID is set)

CONFIG_DIR="${CONFIG_DIR:-/config}"
JOBS_FILE="${JOBS_FILE:-$CONFIG_DIR/jobs.json}"
LOG_FILE="${LOG_FILE:-$CONFIG_DIR/logs/sync.log}"
LAST_RUN="${LAST_RUN:-$CONFIG_DIR/last_run.json}"
SYNC_JOB_SCRIPT="/scripts/sync-job.sh"

mkdir -p "$(dirname "$LOG_FILE")"

append_log_line() {
    echo "$1" >> "$LOG_FILE"
    echo "$1" >&2
}

log_separator() {
    append_log_line "$(printf '#%.0s' {1..80})"
}

run_job() {
    local job_id="$1"
    local job_name="$2"
    local source_dir="$3"
    local remote_path="$4"

    export JOB_ID="$job_id"
    export JOB_NAME="$job_name"
    export SOURCE_DIR="$source_dir"
    export REMOTE_PATH="$remote_path"
    export DB_DIR="$CONFIG_DIR/db"
    export LOG_FILE
    export LAST_RUN_FILE="$CONFIG_DIR/db/$job_id/last_run.json"
    export RCLONE_CONFIG="${RCLONE_CONFIG:-$CONFIG_DIR/rclone.conf}"

    bash "$SYNC_JOB_SCRIPT"
}

aggregate_last_run() {
    local ran_ids="${1:-}"
    python3 - "$CONFIG_DIR" "$LAST_RUN" "$ran_ids" <<'PY'
import json, sys
from datetime import datetime, timezone
from pathlib import Path

config_dir = Path(sys.argv[1])
out_file = Path(sys.argv[2])
ran_ids_raw = sys.argv[3] if len(sys.argv) > 3 else ""
ran_ids = {x for x in ran_ids_raw.split(",") if x}

configured_ids = set()
jobs_file = config_dir / "jobs.json"
if jobs_file.exists():
    try:
        configured_ids = {j["id"] for j in json.loads(jobs_file.read_text()).get("jobs", [])}
    except Exception:
        pass

jobs = []
total_changed = total_deleted = total_folders = 0
any_error = any_success = False

for last_run in sorted(config_dir.glob("db/*/last_run.json")):
    job_id = last_run.parent.name
    if ran_ids and job_id not in ran_ids:
        continue
    if configured_ids and job_id not in configured_ids:
        continue
    try:
        data = json.loads(last_run.read_text())
    except Exception:
        continue
    jobs.append(data)
    total_changed += int(data.get("new_changed", 0) or 0)
    total_deleted += int(data.get("deleted", 0) or 0)
    total_folders += int(data.get("deleted_folders", 0) or 0)
    if data.get("status") == "error":
        any_error = True
    if data.get("status") == "success":
        any_success = True

if not jobs:
    sys.exit(0)

if any_error and any_success:
    status = "partial"
elif any_error:
    status = "error"
else:
    status = "success"

summary = {
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "status": status,
    "new_changed": total_changed,
    "deleted": total_deleted,
    "deleted_folders": total_folders,
    "ran_job_ids": sorted(ran_ids) if ran_ids else sorted(j.get("job_id", "") for j in jobs),
    "jobs": jobs,
}
out_file.write_text(json.dumps(summary, indent=2) + "\n")
PY
}

finalize_sync() {
    local code=$1
    local ran_ids="${2:-}"
    aggregate_last_run "$ran_ids"
    exit "$code"
}

if [ -n "${JOB_ID:-}" ] && [ -n "${SOURCE_DIR:-}" ] && [ -n "${REMOTE_PATH:-}" ]; then
    run_job "${JOB_ID}" "${JOB_NAME:-$JOB_ID}" "$SOURCE_DIR" "$REMOTE_PATH"
    finalize_sync $? "$JOB_ID"
fi

if [ ! -f "$JOBS_FILE" ]; then
    append_log_line "$(date '+%Y-%m-%d %H:%M:%S') - ERROR: No jobs configured ($JOBS_FILE missing)"
    exit 1
fi

append_log_line ""
log_separator
append_log_line "$(date '+%Y-%m-%d %H:%M:%S') - Starting multi-job sync"
log_separator

EXIT_CODE=0
RAN_JOB_IDS=()
while IFS=$'\t' read -r job_id job_name source_dir remote_path; do
    [ -z "$job_id" ] && continue
    RAN_JOB_IDS+=("$job_id")
    if ! run_job "$job_id" "$job_name" "$source_dir" "$remote_path"; then
        EXIT_CODE=1
    fi
done < <(python3 - "$JOBS_FILE" <<'PY'
import json, sys
jobs = json.load(open(sys.argv[1])).get("jobs", [])
for job in jobs:
    if not job.get("enabled", True):
        continue
    print("\t".join([
        job.get("id", ""),
        job.get("name", job.get("id", "")),
        job.get("source_dir", ""),
        job.get("remote_path", ""),
    ]))
PY
)

RAN_IDS_CSV=$(IFS=,; echo "${RAN_JOB_IDS[*]}")
finalize_sync "$EXIT_CODE" "$RAN_IDS_CSV"
