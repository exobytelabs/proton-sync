#!/usr/bin/env python3
"""
Proton Drive Sync Manager — Flask API backend
"""
import os
import json
import re
import secrets
import shlex
import shutil
import sqlite3
import subprocess
import threading
import uuid
import configparser
from datetime import datetime, timezone, timedelta
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request, send_from_directory, session, Response
from crontab import CronTab

from config_zip import build_config_zip, export_filename, import_config_zip
from stats_history import build_chart_data

app = Flask(__name__, static_folder="/app/static", static_url_path="")

CONFIG_DIR   = Path(os.environ.get("CONFIG_DIR",  "/config"))
UI_USERNAME  = os.environ.get("UI_USERNAME", "admin")
UI_PASSWORD  = os.environ.get("UI_PASSWORD", "")
AUTH_ENABLED = bool(UI_PASSWORD)
RCLONE_CONF  = CONFIG_DIR / "rclone.conf"
JOBS_FILE    = CONFIG_DIR / "jobs.json"
LOG_FILE     = CONFIG_DIR / "logs" / "sync.log"
LAST_RUN     = CONFIG_DIR / "last_run.json"
SYNC_SCRIPT  = "/scripts/sync.sh"
CRON_COMMENT = "proton-sync-manager"
JOB_ID_RE    = re.compile(r"^[a-zA-Z0-9_-]+$")
REMOTE_PREFIX = "protondrive:"
DEFAULT_SOURCE_DIR = "/data"
DEFAULT_REMOTE_PATH = "protondrive:NAS-Backup"
OVERRIDES_FILE = CONFIG_DIR / "overrides.env"
DATA_ROOT = Path(DEFAULT_SOURCE_DIR)

sync_proc  = None
sync_running = False


def get_secret_key() -> str:
    if key := os.environ.get("SECRET_KEY"):
        return key
    key_file = CONFIG_DIR / "secret.key"
    if key_file.exists():
        return key_file.read_text().strip()
    key = secrets.token_hex(32)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    key_file.write_text(key)
    return key


app.secret_key = get_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=86400 * 7,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def ui_authenticated() -> bool:
    return not AUTH_ENABLED or session.get("authenticated") is True


def require_ui_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if ui_authenticated():
            return f(*args, **kwargs)
        return jsonify({"error": "Unauthorized"}), 401
    return wrapper

def rclone_configured() -> bool:
    if not RCLONE_CONF.exists():
        return False
    cfg = configparser.ConfigParser()
    cfg.read(RCLONE_CONF)
    return any(cfg[s].get("type") == "protondrive" for s in cfg.sections())

def get_remote_name() -> str | None:
    if not RCLONE_CONF.exists():
        return None
    cfg = configparser.ConfigParser()
    cfg.read(RCLONE_CONF)
    for s in cfg.sections():
        if cfg[s].get("type") == "protondrive":
            return s
    return None

def normalize_schedule(schedule: str | None) -> str | None:
    if schedule is None:
        return None
    schedule = schedule.strip()
    return schedule or None


def cron_comment_for(job_id: str) -> str:
    return f"{CRON_COMMENT}:{job_id}"


def job_cron_command(job: dict) -> str:
    log_file = CONFIG_DIR / "logs" / "cron.log"
    return (
        f"JOB_ID={shlex.quote(job['id'])} "
        f"JOB_NAME={shlex.quote(job['name'])} "
        f"SOURCE_DIR={shlex.quote(job['source_dir'])} "
        f"REMOTE_PATH={shlex.quote(job['remote_path'])} "
        f"CONFIG_DIR={shlex.quote(str(CONFIG_DIR))} "
        f"JOBS_FILE={shlex.quote(str(JOBS_FILE))} "
        f"bash {SYNC_SCRIPT} >> {shlex.quote(str(log_file))} 2>&1"
    )


def sync_job_crons(jobs: list[dict]):
    cron = CronTab(user=True)
    cron.remove_all(comment=CRON_COMMENT)
    for entry in list(cron):
        if entry.comment and entry.comment.startswith(f"{CRON_COMMENT}:"):
            cron.remove(entry)

    for job in jobs:
        schedule = normalize_schedule(job.get("schedule"))
        if not schedule or not job.get("enabled", True):
            continue
        entry = cron.new(
            command=job_cron_command(job),
            comment=cron_comment_for(job["id"]),
        )
        entry.setall(schedule)
    cron.write()


def _read_overrides() -> dict[str, str]:
    overrides: dict[str, str] = {}
    if not OVERRIDES_FILE.exists():
        return overrides
    for line in OVERRIDES_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        overrides[key.strip()] = value.strip()
    return overrides


def paths_locked() -> bool:
    return os.environ.get("PROTON_SYNC_PATHS_LOCKED", "").lower() in ("1", "true", "yes")


def resolve_path_settings() -> dict:
    overrides = _read_overrides()
    env_source = os.environ.get("SOURCE_DIR", DEFAULT_SOURCE_DIR)
    env_remote = os.environ.get("REMOTE_PATH", DEFAULT_REMOTE_PATH)
    locked = paths_locked()

    if locked:
        source_dir = env_source
        remote_path = env_remote
        source = "compose"
    else:
        source_dir = overrides.get("SOURCE_DIR", env_source)
        remote_path = overrides.get("REMOTE_PATH", env_remote)
        source = "overrides" if overrides else "default"

    remote_path = normalize_remote_path(display_remote_path(remote_path))
    return {
        "source_dir": source_dir,
        "remote_path": remote_path,
        "remote_folder": display_remote_path(remote_path),
        "locked": locked,
        "source": source,
    }


def reload_path_settings() -> dict:
    global DATA_ROOT
    settings = resolve_path_settings()
    os.environ["SOURCE_DIR"] = settings["source_dir"]
    os.environ["REMOTE_PATH"] = settings["remote_path"]
    DATA_ROOT = Path(settings["source_dir"])
    return settings


def _write_overrides(source_dir: str, remote_path: str) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    OVERRIDES_FILE.write_text(
        "\n".join([
            "# Proton Sync path settings (managed via Settings UI)",
            f"SOURCE_DIR={source_dir}",
            f"REMOTE_PATH={remote_path}",
            "",
        ])
    )


def _default_job() -> dict:
    settings = resolve_path_settings()
    return {
        "id": "default",
        "name": "Main Backup",
        "source_dir": settings["source_dir"],
        "remote_path": settings["remote_path"],
        "enabled": True,
        "schedule": None,
    }


def load_jobs() -> list[dict]:
    if JOBS_FILE.exists():
        try:
            data = json.loads(JOBS_FILE.read_text())
            jobs = data.get("jobs", [])
            if jobs:
                for job in jobs:
                    job.setdefault("schedule", None)
                return jobs
        except Exception:
            pass
    job = _default_job()
    save_jobs([job])
    return [job]


def save_jobs(jobs: list[dict]):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    JOBS_FILE.write_text(json.dumps({"jobs": jobs}, indent=2) + "\n")
    sync_job_crons(jobs)


def find_job(job_id: str) -> dict | None:
    return next((j for j in load_jobs() if j.get("id") == job_id), None)


def normalize_remote_path(path: str) -> str:
    path = path.strip().lstrip("/")
    if path.startswith(REMOTE_PREFIX):
        return path
    return f"{REMOTE_PREFIX}{path}"


def display_remote_path(path: str) -> str:
    if path.startswith(REMOTE_PREFIX):
        return path[len(REMOTE_PREFIX):]
    return path


def default_remote_prefix() -> str:
    return resolve_path_settings()["remote_folder"].rstrip("/")


def resolve_browse_path(path: str | None) -> Path | None:
    target = DATA_ROOT if not path else Path(path)
    if not target.is_absolute():
        return None
    try:
        resolved = target.resolve()
        root = DATA_ROOT.resolve()
        resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    if not resolved.is_dir():
        return None
    return resolved


def list_browse_entries(directory: Path) -> list[dict]:
    entries: list[dict] = []
    try:
        with os.scandir(directory) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        entries.append({
                            "name": entry.name,
                            "type": "dir",
                            "path": str(Path(entry.path).resolve()),
                        })
                    elif entry.is_file(follow_symlinks=False):
                        entries.append({
                            "name": entry.name,
                            "type": "file",
                            "path": str(Path(entry.path).resolve()),
                            "size": entry.stat(follow_symlinks=False).st_size,
                        })
                except OSError:
                    continue
    except OSError:
        return []
    entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
    return entries


def suggest_remote_folder(source_dir: str) -> str:
    root = DATA_ROOT.resolve()
    try:
        rel = Path(source_dir).resolve().relative_to(root)
        suffix = rel.as_posix()
    except ValueError:
        suffix = Path(source_dir).name
    prefix = default_remote_prefix()
    return f"{prefix}/{suffix}" if suffix and suffix != "." else prefix


def job_for_api(job: dict) -> dict:
    return {**job, "remote_folder": display_remote_path(job.get("remote_path", ""))}


def scan_source_stats(source_dir: str) -> dict:
    directory = resolve_browse_path(source_dir)
    if not directory:
        return {"files": 0, "folders": 0, "bytes": 0}
    files = folders = total_bytes = 0
    for root, dirs, filenames in os.walk(directory):
        folders += len(dirs)
        for name in filenames:
            files += 1
            try:
                total_bytes += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return {"files": files, "folders": folders, "bytes": total_bytes}


def job_tracker_stats(job_id: str, source_dir: str) -> dict:
    db_file = CONFIG_DIR / "db" / job_id / "file_tracker.db"
    folder_db = CONFIG_DIR / "db" / job_id / "folder_tracker.db"
    if not db_file.exists():
        return scan_source_stats(source_dir)

    files = folders = total_bytes = 0
    try:
        with sqlite3.connect(f"file:{db_file}?mode=ro", uri=True) as conn:
            files = int(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0])
            total_bytes = int(conn.execute("SELECT COALESCE(SUM(size), 0) FROM files").fetchone()[0])
    except Exception:
        return scan_source_stats(source_dir)

    if folder_db.exists():
        try:
            with sqlite3.connect(f"file:{folder_db}?mode=ro", uri=True) as conn:
                folders = int(conn.execute("SELECT COUNT(*) FROM folders").fetchone()[0])
        except Exception:
            pass

    return {"files": files, "folders": folders, "bytes": total_bytes}


def uploaded_today_by_job() -> dict[str, int]:
    if not LOG_FILE.exists():
        return {}

    tz_name = os.environ.get("TZ", "UTC")
    try:
        today = datetime.now(ZoneInfo(tz_name)).date()
    except Exception:
        today = datetime.now(timezone.utc).date()

    totals: dict[str, int] = {}
    pattern = re.compile(
        r"^(\d{4}-\d{2}-\d{2}) \d{2}:\d{2}:\d{2} - \[([^\]]+)\] Sync completed \| new/changed=(\d+)"
    )
    try:
        lines = LOG_FILE.read_text(errors="replace").splitlines()
    except OSError:
        return {}

    for line in lines:
        match = pattern.match(line)
        if not match:
            continue
        try:
            log_date = datetime.strptime(match.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue
        if log_date != today:
            continue
        job_name = match.group(2)
        totals[job_name] = totals.get(job_name, 0) + int(match.group(3))
    return totals


def job_for_api_with_stats(job: dict, uploaded_today: dict[str, int]) -> dict:
    stats = job_tracker_stats(job["id"], job["source_dir"])
    return {
        **job_for_api(job),
        "stats": {
            **stats,
            "uploaded_today": int(uploaded_today.get(job.get("name", ""), 0)),
        },
    }


def validate_job_payload(
    data: dict,
    *,
    existing_id: str | None = None,
    existing_job: dict | None = None,
) -> tuple[dict | None, str | None]:
    name = (data.get("name") or "").strip()
    source_dir = (data.get("source_dir") or "").strip()
    remote_input = (data.get("remote_path") or data.get("remote_folder") or "").strip()
    job_id = (data.get("id") or existing_id or "").strip() or f"job-{uuid.uuid4().hex[:8]}"

    if not name:
        return None, "name is required"
    if not source_dir.startswith("/"):
        return None, "source_dir must be an absolute path"
    if resolve_browse_path(source_dir) is None:
        return None, "source_dir does not exist or is outside the data volume"
    if not remote_input or remote_input == REMOTE_PREFIX.rstrip(":"):
        return None, "remote folder is required"
    if not JOB_ID_RE.match(job_id):
        return None, "id may only contain letters, numbers, underscores, and hyphens"

    if "schedule" in data:
        schedule = normalize_schedule(data.get("schedule"))
    elif existing_job:
        schedule = existing_job.get("schedule")
    else:
        schedule = None

    if schedule:
        try:
            CronTab().new(command="true", comment="validate").setall(schedule)
        except Exception as e:
            return None, f"invalid schedule: {e}"

    job = {
        "id": job_id,
        "name": name,
        "source_dir": source_dir,
        "remote_path": normalize_remote_path(remote_input),
        "enabled": bool(data.get("enabled", True)),
        "schedule": schedule,
    }
    return job, None


def _parse_iso_ts(value: str) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
    except Exception:
        return None


def sanitize_last_run(data: dict) -> dict:
    """Keep dashboard totals aligned with configured jobs and the latest sync session."""
    current_ids = {j["id"] for j in load_jobs()}
    jobs = list(data.get("jobs") or [])
    ran_ids = set(data.get("ran_job_ids") or [])

    jobs = [j for j in jobs if j.get("job_id") in current_ids]
    if ran_ids:
        jobs = [j for j in jobs if j.get("job_id") in ran_ids]
    elif data.get("timestamp"):
        agg_ts = _parse_iso_ts(data["timestamp"])
        if agg_ts:
            window = timedelta(minutes=5)
            jobs = [
                j for j in jobs
                if (jt := _parse_iso_ts(j.get("timestamp", "")))
                and abs(agg_ts - jt) <= window
            ]

    data = dict(data)
    data["jobs"] = jobs
    data["new_changed"] = sum(int(j.get("new_changed", 0) or 0) for j in jobs)
    data["deleted"] = sum(int(j.get("deleted", 0) or 0) for j in jobs)
    data["deleted_folders"] = sum(int(j.get("deleted_folders", 0) or 0) for j in jobs)
    return data


def reaggregate_last_run(ran_job_ids: list[str] | None = None) -> None:
    """Rebuild last_run.json from per-job summaries for configured jobs only."""
    configured_ids = {j["id"] for j in load_jobs()}
    ran_ids = set(ran_job_ids or [])
    jobs = []
    total_changed = total_deleted = total_folders = 0
    any_error = any_success = False

    for last_run in sorted((CONFIG_DIR / "db").glob("*/last_run.json")):
        job_id = last_run.parent.name
        if ran_ids and job_id not in ran_ids:
            continue
        if configured_ids and job_id not in configured_ids:
            continue
        try:
            entry = json.loads(last_run.read_text())
        except Exception:
            continue
        jobs.append(entry)
        total_changed += int(entry.get("new_changed", 0) or 0)
        total_deleted += int(entry.get("deleted", 0) or 0)
        total_folders += int(entry.get("deleted_folders", 0) or 0)
        if entry.get("status") == "error":
            any_error = True
        if entry.get("status") == "success":
            any_success = True

    if not jobs:
        if LAST_RUN.exists():
            LAST_RUN.unlink()
        return

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
    LAST_RUN.write_text(json.dumps(summary, indent=2) + "\n")


def load_last_run() -> dict | None:
    if not LAST_RUN.exists():
        return None
    try:
        return sanitize_last_run(json.loads(LAST_RUN.read_text()))
    except Exception:
        return None


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("/app/static", "index.html")


# ── UI login ──────────────────────────────────────────────────────────────────

@app.route("/api/session")
def ui_session():
    return jsonify({
        "auth_enabled": AUTH_ENABLED,
        "authenticated": ui_authenticated(),
        "username": session.get("username") if ui_authenticated() else None,
    })


@app.route("/api/login", methods=["POST"])
def ui_login():
    if not AUTH_ENABLED:
        session["authenticated"] = True
        return jsonify({"status": "ok", "username": UI_USERNAME})

    data = request.json or {}
    username = data.get("username", "")
    password = data.get("password", "")

    if (secrets.compare_digest(username, UI_USERNAME)
            and secrets.compare_digest(password, UI_PASSWORD)):
        session.permanent = True
        session["authenticated"] = True
        session["username"] = UI_USERNAME
        return jsonify({"status": "ok", "username": UI_USERNAME})

    return jsonify({"error": "Invalid username or password"}), 401


@app.route("/api/logout", methods=["POST"])
def ui_logout():
    session.clear()
    return jsonify({"status": "ok"})


@app.route("/api/status")
@require_ui_auth
def status():
    uploaded_today = uploaded_today_by_job()
    return jsonify({
        "rclone_configured": rclone_configured(),
        "remote_name": get_remote_name(),
        "sync_running": sync_running,
        "jobs": [job_for_api_with_stats(j, uploaded_today) for j in load_jobs()],
        "last_run": load_last_run(),
    })


@app.route("/api/settings/paths")
@require_ui_auth
def settings_paths_get():
    return jsonify(reload_path_settings())


@app.route("/api/settings/paths", methods=["PUT"])
@require_ui_auth
def settings_paths_put():
    if paths_locked():
        return jsonify({"error": "Paths are locked by docker-compose (PROTON_SYNC_PATHS_LOCKED)"}), 403

    data = request.json or {}
    source_dir = (data.get("source_dir") or "").strip()
    remote_input = (data.get("remote_folder") or data.get("remote_path") or "").strip()
    if not source_dir.startswith("/"):
        return jsonify({"error": "source_dir must be an absolute path"}), 400
    if not remote_input:
        return jsonify({"error": "remote_folder is required"}), 400

    directory = Path(source_dir)
    if not directory.is_absolute():
        return jsonify({"error": "source_dir must be an absolute path"}), 400

    remote_path = normalize_remote_path(remote_input)
    _write_overrides(source_dir, remote_path)
    settings = reload_path_settings()
    return jsonify({"status": "ok", **settings})


@app.route("/api/stats/history")
@require_ui_auth
def stats_history():
    days = request.args.get("days", default=30, type=int)
    days = max(7, min(days, 90))
    return jsonify(build_chart_data(
        CONFIG_DIR,
        log_file=LOG_FILE,
        jobs=load_jobs(),
        days=days,
    ))

# ── Auth / rclone config ──────────────────────────────────────────────────────

_auth_output = []
_auth_lock = threading.Lock()

@app.route("/api/auth/start", methods=["POST"])
@require_ui_auth
def auth_start():
    global _auth_output
    data = request.json or {}
    username = data.get("username", "")
    password = data.get("password", "")

    if not username or not password:
        return jsonify({"error": "username and password required"}), 400

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _auth_output = []

    def run():
        obscured = subprocess.check_output(
            ["rclone", "obscure", password], text=True
        ).strip()

        conf_lines = [
            "[protondrive]",
            "type = protondrive",
            f"username = {username}",
            f"password = {obscured}",
        ]

        RCLONE_CONF.write_text("\n".join(conf_lines) + "\n")
        _auth_output.append({"type": "info", "msg": "Config written. Testing connection…"})

        result = subprocess.run(
            ["rclone", "lsd", "protondrive:", "--config", str(RCLONE_CONF)],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode == 0:
            _auth_output.append({"type": "success", "msg": "Connected to Proton Drive!"})
        else:
            err = result.stderr.strip() or "Connection failed"
            _auth_output.append({"type": "error", "msg": err})

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return jsonify({"status": "started"})

@app.route("/api/auth/poll")
@require_ui_auth
def auth_poll():
    with _auth_lock:
        msgs = list(_auth_output)
    done = any(m["type"] in ("success", "error") for m in msgs)
    return jsonify({"messages": msgs, "done": done,
                    "configured": rclone_configured()})

@app.route("/api/auth/clear", methods=["POST"])
@require_ui_auth
def auth_clear():
    if RCLONE_CONF.exists():
        RCLONE_CONF.unlink()
    if LAST_RUN.exists():
        LAST_RUN.unlink()
    return jsonify({"status": "cleared"})

# ── Sync ──────────────────────────────────────────────────────────────────────

@app.route("/api/sync/start", methods=["POST"])
@require_ui_auth
def sync_start():
    global sync_running, sync_proc
    if sync_running:
        return jsonify({"error": "Sync already running"}), 409

    data = request.json or {}
    job_id = data.get("job_id")
    env = os.environ.copy()
    env["RCLONE_CONFIG"] = str(RCLONE_CONF)
    env["JOBS_FILE"] = str(JOBS_FILE)
    env["LOG_FILE"] = str(LOG_FILE)
    env["CONFIG_DIR"] = str(CONFIG_DIR)

    if job_id:
        job = find_job(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        env["JOB_ID"] = job["id"]
        env["JOB_NAME"] = job["name"]
        env["SOURCE_DIR"] = job["source_dir"]
        env["REMOTE_PATH"] = job["remote_path"]

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    def run():
        global sync_running, sync_proc
        sync_running = True
        try:
            sync_proc = subprocess.Popen(
                ["bash", SYNC_SCRIPT],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
            )
            sync_proc.wait()
            if sync_proc.returncode != 0:
                summary = load_last_run() or {}
                summary["status"] = "error"
                LAST_RUN.write_text(json.dumps(summary, indent=2) + "\n")
        finally:
            sync_running = False
            sync_proc = None

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"status": "started", "job_id": job_id})

@app.route("/api/sync/stop", methods=["POST"])
@require_ui_auth
def sync_stop():
    global sync_proc
    if sync_proc:
        sync_proc.terminate()
    return jsonify({"status": "stopped"})

# ── Logs ──────────────────────────────────────────────────────────────────────

@app.route("/api/logs")
@require_ui_auth
def logs():
    lines = int(request.args.get("lines", 200))
    if not LOG_FILE.exists():
        return jsonify({"lines": []})
    with open(LOG_FILE) as f:
        content = f.readlines()
    return jsonify({"lines": [l.rstrip() for l in content[-lines:]]})

@app.route("/api/logs/clear", methods=["POST"])
@require_ui_auth
def logs_clear():
    if LOG_FILE.exists():
        LOG_FILE.write_text("")
    return jsonify({"status": "cleared"})

# ── File browser ──────────────────────────────────────────────────────────────

@app.route("/api/browse")
@require_ui_auth
def browse_data():
    path = (request.args.get("path") or "").strip() or None
    directory = resolve_browse_path(path)
    if directory is None:
        return jsonify({"error": "Invalid or inaccessible path"}), 400

    root = DATA_ROOT.resolve()
    parent = str(directory.parent) if directory != root else None

    return jsonify({
        "path": str(directory),
        "parent": parent,
        "root": str(root),
        "default_remote_prefix": default_remote_prefix(),
        "suggested_remote_folder": suggest_remote_folder(str(directory)),
        "entries": list_browse_entries(directory),
    })

# ── Backup jobs ───────────────────────────────────────────────────────────────

@app.route("/api/jobs", methods=["POST"])
@require_ui_auth
def jobs_create():
    job, error = validate_job_payload(request.json or {})
    if error:
        return jsonify({"error": error}), 400

    jobs = load_jobs()
    if any(j.get("id") == job["id"] for j in jobs):
        return jsonify({"error": "A job with this id already exists"}), 409
    jobs.append(job)
    try:
        save_jobs(jobs)
    except Exception as e:
        jobs.pop()
        return jsonify({"error": str(e)}), 400
    return jsonify({"status": "ok", "job": job_for_api(job)}), 201


@app.route("/api/jobs/<job_id>", methods=["PUT"])
@require_ui_auth
def jobs_update(job_id):
    jobs = load_jobs()
    idx = next((i for i, j in enumerate(jobs) if j.get("id") == job_id), None)
    if idx is None:
        return jsonify({"error": "Job not found"}), 404

    payload = request.json or {}
    payload["id"] = job_id
    job, error = validate_job_payload(payload, existing_id=job_id, existing_job=jobs[idx])
    if error:
        return jsonify({"error": error}), 400

    jobs[idx] = job
    try:
        save_jobs(jobs)
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"status": "ok", "job": job_for_api(job)})


@app.route("/api/jobs/<job_id>", methods=["DELETE"])
@require_ui_auth
def jobs_delete(job_id):
    jobs = load_jobs()
    remaining = [j for j in jobs if j.get("id") != job_id]
    if len(remaining) == len(jobs):
        return jsonify({"error": "Job not found"}), 404
    if not remaining:
        return jsonify({"error": "At least one backup job is required"}), 400
    try:
        save_jobs(remaining)
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    job_db = CONFIG_DIR / "db" / job_id
    if job_db.exists():
        shutil.rmtree(job_db)
    reaggregate_last_run()
    return jsonify({"status": "ok"})


# ── Config backup (zip) ─────────────────────────────────────────────────────────

@app.route("/api/config/export")
@require_ui_auth
def config_export():
    if not RCLONE_CONF.exists() and not JOBS_FILE.exists():
        return jsonify({"error": "No config to export"}), 400
    filename = export_filename()
    return Response(
        build_config_zip(CONFIG_DIR),
        mimetype="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/api/config/import", methods=["POST"])
@require_ui_auth
def config_import():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    upload = request.files["file"]
    if not upload.filename:
        return jsonify({"error": "No file selected"}), 400

    only_if_empty = request.args.get("only_if_empty", "").lower() in ("1", "true", "yes")
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    staging = CONFIG_DIR / "import-upload.zip"
    upload.save(staging)

    try:
        result = import_config_zip(staging, only_if_empty=only_if_empty)
    finally:
        if staging.exists():
            staging.unlink()

    if result["status"] == "error":
        return jsonify({"error": result["message"]}), 400
    if result["status"] == "skipped":
        return jsonify(result), 409

    try:
        reload_path_settings()
        sync_job_crons(load_jobs())
    except Exception as e:
        return jsonify({"error": f"Config imported but cron sync failed: {e}"}), 500

    return jsonify(result)


try:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    (CONFIG_DIR / "logs").mkdir(exist_ok=True)
    (CONFIG_DIR / "db").mkdir(exist_ok=True)
    reload_path_settings()
    sync_job_crons(load_jobs())
except Exception:
    pass
