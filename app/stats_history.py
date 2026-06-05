#!/usr/bin/env python3
"""Persist and serve sync history for dashboard charts."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

LOG_COMPLETION_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) - \[([^\]]+)\] Sync completed \| "
    r"new/changed=(\d+) \| deleted=(\d+)(?: \| deleted_folders=(\d+))?"
)


def stats_dir(config_dir: Path) -> Path:
    path = config_dir / "stats"
    path.mkdir(parents=True, exist_ok=True)
    return path


def history_file(config_dir: Path) -> Path:
    return stats_dir(config_dir) / "history.jsonl"


def backfill_marker(config_dir: Path) -> Path:
    return stats_dir(config_dir) / ".log_backfill_v1"


def read_tracker_stats(db_dir: Path) -> dict:
    db_file = db_dir / "file_tracker.db"
    folder_db = db_dir / "folder_tracker.db"
    files = folders = total_bytes = 0

    if db_file.exists():
        try:
            with sqlite3.connect(f"file:{db_file}?mode=ro", uri=True) as conn:
                files = int(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0])
                total_bytes = int(conn.execute("SELECT COALESCE(SUM(size), 0) FROM files").fetchone()[0])
        except Exception:
            pass

    if folder_db.exists():
        try:
            with sqlite3.connect(f"file:{folder_db}?mode=ro", uri=True) as conn:
                folders = int(conn.execute("SELECT COUNT(*) FROM folders").fetchone()[0])
        except Exception:
            pass

    return {"files": files, "folders": folders, "bytes": total_bytes}


def load_jobs_by_name(config_dir: Path) -> dict[str, str]:
    jobs_file = config_dir / "jobs.json"
    if not jobs_file.exists():
        return {}
    try:
        jobs = json.loads(jobs_file.read_text()).get("jobs", [])
    except Exception:
        return {}
    return {j.get("name", ""): j.get("id", "") for j in jobs if j.get("name")}


def append_entry(config_dir: Path, entry: dict) -> None:
    path = history_file(config_dir)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, separators=(",", ":")) + "\n")


def load_entries(config_dir: Path) -> list[dict]:
    path = history_file(config_dir)
    if not path.exists():
        return []
    entries: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def parse_log_timestamp(raw: str, tz_name: str) -> str:
    naive = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    try:
        local = naive.replace(tzinfo=ZoneInfo(tz_name))
        return local.astimezone(timezone.utc).isoformat()
    except Exception:
        return naive.replace(tzinfo=timezone.utc).isoformat()


def backfill_from_log(config_dir: Path, log_file: Path) -> int:
    if backfill_marker(config_dir).exists() or not log_file.exists():
        return 0

    tz_name = os.environ.get("TZ", "UTC")
    jobs_by_name = load_jobs_by_name(config_dir)
    added = 0

    for line in log_file.read_text(encoding="utf-8", errors="replace").splitlines():
        match = LOG_COMPLETION_RE.match(line)
        if not match:
            continue
        job_name = match.group(2)
        entry = {
            "timestamp": parse_log_timestamp(match.group(1), tz_name),
            "job_id": jobs_by_name.get(job_name, job_name),
            "job_name": job_name,
            "new_changed": int(match.group(3)),
            "deleted": int(match.group(4)),
            "deleted_folders": int(match.group(5) or 0),
            "source": "log",
        }
        append_entry(config_dir, entry)
        added += 1

    backfill_marker(config_dir).touch()
    return added


def record_sync_event(
    config_dir: Path,
    *,
    job_id: str,
    job_name: str,
    new_changed: int,
    deleted: int,
    deleted_folders: int,
    db_dir: Path,
) -> dict:
    tracker = read_tracker_stats(db_dir)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "job_id": job_id,
        "job_name": job_name,
        "bytes": tracker["bytes"],
        "files": tracker["files"],
        "folders": tracker["folders"],
        "new_changed": int(new_changed),
        "deleted": int(deleted),
        "deleted_folders": int(deleted_folders),
        "source": "sync",
    }
    append_entry(config_dir, entry)
    return entry


def seed_current_storage(config_dir: Path, jobs: list[dict]) -> None:
    """Add a baseline storage point when no sync snapshots exist yet."""
    entries = load_entries(config_dir)
    if any("bytes" in e for e in entries):
        return
    if not jobs:
        return

    timestamp = datetime.now(timezone.utc).isoformat()
    for job in jobs:
        db_dir = config_dir / "db" / job["id"]
        tracker = read_tracker_stats(db_dir)
        append_entry(config_dir, {
            "timestamp": timestamp,
            "job_id": job["id"],
            "job_name": job.get("name", job["id"]),
            "bytes": tracker["bytes"],
            "files": tracker["files"],
            "folders": tracker["folders"],
            "new_changed": 0,
            "deleted": 0,
            "deleted_folders": 0,
            "source": "snapshot",
        })


def _parse_ts(value: str) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
    except Exception:
        return None


def _local_date(value: str, tz_name: str) -> str | None:
    ts = _parse_ts(value)
    if not ts:
        return None
    try:
        return ts.astimezone(ZoneInfo(tz_name)).date().isoformat()
    except Exception:
        return ts.date().isoformat()


def build_chart_data(
    config_dir: Path,
    *,
    log_file: Path,
    jobs: list[dict],
    days: int = 30,
) -> dict:
    backfill_from_log(config_dir, log_file)
    seed_current_storage(config_dir, jobs)

    tz_name = os.environ.get("TZ", "UTC")
    try:
        today = datetime.now(ZoneInfo(tz_name)).date()
    except Exception:
        today = datetime.now(timezone.utc).date()
    cutoff = today - timedelta(days=max(days - 1, 0))

    entries = load_entries(config_dir)
    entries.sort(key=lambda e: e.get("timestamp", ""))

    latest_by_job: dict[str, dict] = {}
    storage_points: list[dict] = []
    activity_points: list[dict] = []
    daily: dict[str, dict] = {}

    for entry in entries:
        ts = entry.get("timestamp", "")
        parsed = _parse_ts(ts)
        if not parsed:
            continue

        local_date = _local_date(ts, tz_name)
        if local_date and local_date < cutoff.isoformat():
            continue

        activity_points.append({
            "timestamp": ts,
            "job_id": entry.get("job_id", ""),
            "job_name": entry.get("job_name", ""),
            "new_changed": int(entry.get("new_changed", 0) or 0),
            "deleted": int(entry.get("deleted", 0) or 0),
        })

        if local_date:
            bucket = daily.setdefault(local_date, {
                "date": local_date,
                "new_changed": 0,
                "deleted": 0,
                "syncs": 0,
            })
            bucket["new_changed"] += int(entry.get("new_changed", 0) or 0)
            bucket["deleted"] += int(entry.get("deleted", 0) or 0)
            bucket["syncs"] += 1

        if "bytes" not in entry:
            continue
        job_id = entry.get("job_id", "")
        if not job_id:
            continue
        latest_by_job[job_id] = entry
        storage_points.append({
            "timestamp": ts,
            "bytes": sum(int(j.get("bytes", 0) or 0) for j in latest_by_job.values()),
            "files": sum(int(j.get("files", 0) or 0) for j in latest_by_job.values()),
            "folders": sum(int(j.get("folders", 0) or 0) for j in latest_by_job.values()),
        })

    deduped_storage: list[dict] = []
    for point in storage_points:
        if deduped_storage and deduped_storage[-1]["timestamp"] == point["timestamp"]:
            deduped_storage[-1] = point
        else:
            deduped_storage.append(point)

    return {
        "days": days,
        "storage": deduped_storage,
        "activity": activity_points,
        "activity_daily": [daily[k] for k in sorted(daily.keys())],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Record sync stats history")
    parser.add_argument("--config-dir", default=os.environ.get("CONFIG_DIR", "/config"))
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--job-name", required=True)
    parser.add_argument("--new-changed", type=int, default=0)
    parser.add_argument("--deleted", type=int, default=0)
    parser.add_argument("--deleted-folders", type=int, default=0)
    parser.add_argument("--db-dir", required=True)
    args = parser.parse_args()

    record_sync_event(
        Path(args.config_dir),
        job_id=args.job_id,
        job_name=args.job_name,
        new_changed=args.new_changed,
        deleted=args.deleted,
        deleted_folders=args.deleted_folders,
        db_dir=Path(args.db_dir),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
