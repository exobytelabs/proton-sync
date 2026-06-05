#!/usr/bin/env python3
"""Zip-based config backup and restore."""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
RCLONE_CONF = CONFIG_DIR / "rclone.conf"
JOBS_FILE = CONFIG_DIR / "jobs.json"

ROOT_FILES = ("rclone.conf", "jobs.json", "secret.key", "overrides.env", "last_run.json")
REQUIRED_FILES = ("rclone.conf", "jobs.json")
MANIFEST_VERSION = 1


def config_is_populated() -> bool:
    return RCLONE_CONF.is_file() and JOBS_FILE.is_file()


def export_filename() -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"proton-sync-config-{stamp}.zip"


def build_config_zip(config_dir: Path | None = None) -> bytes:
    root = config_dir or CONFIG_DIR
    buf = io.BytesIO()
    manifest = {
        "version": MANIFEST_VERSION,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2) + "\n")

        for name in ROOT_FILES:
            path = root / name
            if path.is_file():
                zf.write(path, name)

        db_dir = root / "db"
        if db_dir.is_dir():
            for file_path in db_dir.rglob("*"):
                if file_path.is_file():
                    zf.write(file_path, file_path.relative_to(root).as_posix())

    return buf.getvalue()


def _safe_extract_member(zf: zipfile.ZipFile, member: zipfile.ZipInfo, dest: Path) -> None:
    target = (dest / member.filename).resolve()
    if not str(target).startswith(str(dest.resolve()) + os.sep) and target != dest.resolve():
        raise ValueError(f"Unsafe path in archive: {member.filename}")
    if member.is_dir() or member.filename.endswith("/"):
        target.mkdir(parents=True, exist_ok=True)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    with zf.open(member) as src, open(target, "wb") as out:
        shutil.copyfileobj(src, out)


def _validate_archive(zf: zipfile.ZipFile) -> None:
    names = {info.filename for info in zf.infolist() if not info.is_dir()}
    for required in REQUIRED_FILES:
        if required not in names:
            raise ValueError(f"Archive missing required file: {required}")


def import_config_zip(
    zip_path: Path,
    *,
    only_if_empty: bool = False,
    config_dir: Path | None = None,
) -> dict:
    root = config_dir or CONFIG_DIR
    zip_path = zip_path.resolve()

    if not zip_path.is_file():
        return {"status": "error", "message": f"Backup file not found: {zip_path}"}

    if only_if_empty and config_is_populated():
        return {"status": "skipped", "message": "Local config already present"}

    try:
        with zipfile.ZipFile(zip_path) as zf:
            _validate_archive(zf)
            root.mkdir(parents=True, exist_ok=True)
            (root / "logs").mkdir(exist_ok=True)
            (root / "db").mkdir(exist_ok=True)

            for member in zf.infolist():
                if member.filename in ("manifest.json",) or member.filename.endswith("/"):
                    continue
                _safe_extract_member(zf, member, root)
    except (zipfile.BadZipFile, ValueError, OSError) as e:
        return {"status": "error", "message": str(e)}

    if zip_path.name == "backup.zip":
        applied = zip_path.with_name("backup.zip.applied")
        zip_path.rename(applied)

    return {"status": "restored", "message": "Config restored from backup zip"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Proton Sync config zip backup/restore")
    sub = parser.add_subparsers(dest="command", required=True)

    import_cmd = sub.add_parser("import", help="Import config from a zip file")
    import_cmd.add_argument("zip_path", type=Path)
    import_cmd.add_argument(
        "--if-empty",
        action="store_true",
        help="Only import when rclone.conf and jobs.json are missing",
    )

    args = parser.parse_args()
    if args.command == "import":
        result = import_config_zip(args.zip_path, only_if_empty=args.if_empty)
        print(json.dumps(result))
        return 0 if result["status"] in ("restored", "skipped") else 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
