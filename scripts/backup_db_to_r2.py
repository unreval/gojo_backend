#!/usr/bin/env python3
"""Upload a nightly logical dump of the Gojo database to the existing R2 bucket.

pg_dump streams into a temp file, gzip streams that file, then
media_storage.put_bytes uploads the gzip bytes. put_bytes holds the
compressed object for the upload call. The uncompressed SQL is not kept
in memory.

Retention lists db-backups/ first, then deletes only
db-backups/gojo_YYYYMMDD_HHMM.sql.gz. chat-media/ and stickers/ are never
deleted. Any listed protected key aborts retention.

The script does not print DATABASE_URL or other secret values.
"""
from __future__ import annotations

import gzip
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
BACKUP_PREFIX = "db-backups/"
PROTECTED_PREFIXES = ("chat-media/", "stickers/")
BACKUP_NAME = re.compile(r"gojo_[0-9]{8}_[0-9]{4}\.sql\.gz\Z")
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
DEFAULT_KEEP = 14
SHANGHAI = ZoneInfo("Asia/Shanghai")

log = logging.getLogger("backup")


def is_protected_key(key: str) -> bool:
    """True when a key is under a prefix this job must not touch."""
    if not isinstance(key, str) or key == "":
        return True
    for prefix in PROTECTED_PREFIXES:
        if key.startswith(prefix) or f"/{prefix}" in f"/{key}":
            return True
    return False


def is_backup_object_key(key: str) -> bool:
    """True only for the nightly dump names this script itself writes."""
    if not isinstance(key, str) or is_protected_key(key):
        return False
    if not key.startswith(BACKUP_PREFIX):
        return False
    name = key[len(BACKUP_PREFIX):]
    if "/" in name or "\\" in name or name.startswith("."):
        return False
    return BACKUP_NAME.fullmatch(name) is not None


def retention_plan(listed_keys, keep, uploaded_key):
    """Return (delete_keys, ignored_keys).

    The uploaded key is kept even when its timestamp sorts as oldest.
    A protected key aborts the plan so the caller deletes nothing.
    """
    if isinstance(keep, bool) or not isinstance(keep, int) or keep < 1:
        raise ValueError("BACKUP_KEEP_COUNT must be an integer >= 1")
    if not is_backup_object_key(uploaded_key):
        raise RuntimeError("refusing retention because the new object key is not a backup key")

    ignored = []
    eligible = []
    for key in listed_keys:
        if is_protected_key(key):
            raise RuntimeError(
                "refusing retention because a protected object was listed: "
                + key
            )
        if is_backup_object_key(key):
            eligible.append(key)
        else:
            ignored.append(key)

    if uploaded_key not in eligible:
        eligible.append(uploaded_key)
    ordered = sorted(set(eligible))
    overflow = ordered[:-keep] if len(ordered) > keep else []
    delete_keys = [key for key in overflow if key != uploaded_key]
    for key in delete_keys:
        if not is_backup_object_key(key) or is_protected_key(key):
            raise RuntimeError(
                "refusing retention because a delete candidate failed the backup-key check"
            )
    return delete_keys, ignored


def load_env_file(path, environ):
    """Fill missing keys from a local env file. Never overrides, never logs values."""
    text = Path(path).read_text(encoding="utf-8")
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            raise RuntimeError(f"invalid env line {lineno}")
        key, value = line.split("=", 1)
        key = key.strip()
        if not ENV_NAME.fullmatch(key):
            raise RuntimeError(f"invalid env name on line {lineno}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key not in environ:
            environ[key] = value


def identifier(value, label):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise RuntimeError(f"{label} is missing or is not a plain identifier")
    return value


def build_dump_command(mode, *, user, database, database_url, compose_file, docker_bin, pg_dump_bin):
    if mode == "compose":
        user = identifier(user, "POSTGRES_USER")
        database = identifier(database, "POSTGRES_DB")
        compose_file = Path(compose_file)
        return [
            docker_bin, "compose",
            "--project-directory", str(compose_file.parent),
            "-f", str(compose_file),
            "exec", "-T", "db",
            "pg_dump",
            "-U", user,
            "-d", database,
            "--no-password",
            "--format=plain",
            "--no-owner",
            "--no-acl",
        ]
    if mode == "pg_dump":
        if not isinstance(database_url, str) or not database_url.startswith(("postgresql://", "postgres://")):
            raise RuntimeError("DATABASE_URL must start with postgresql://")
        return [
            pg_dump_bin,
            "--dbname", database_url,
            "--no-password",
            "--format=plain",
            "--no-owner",
            "--no-acl",
        ]
    raise RuntimeError("BACKUP_DUMP_MODE must be compose or pg_dump")


def redact_command(cmd):
    redacted = []
    hide_next = False
    for part in cmd:
        if hide_next:
            redacted.append("<redacted>")
            hide_next = False
            continue
        if part == "--dbname":
            redacted.append(part)
            hide_next = True
            continue
        if "://" in part:
            redacted.append("<redacted>")
            continue
        redacted.append(part)
    return redacted


def backup_object_key(now=None):
    moment = now or datetime.now(SHANGHAI)
    return f"{BACKUP_PREFIX}gojo_{moment.strftime('%Y%m%d_%H%M')}.sql.gz"


def _which(name, extra):
    found = shutil.which(name)
    if found:
        return found
    for candidate in extra:
        if Path(candidate).is_file():
            return candidate
    raise RuntimeError(f"{name} not found")


def _backend_path():
    backend = ROOT / "gojo_backend"
    entry = str(backend)
    if entry not in sys.path:
        sys.path.insert(0, entry)


def _storage():
    _backend_path()
    import media_storage
    if not media_storage.is_configured():
        raise RuntimeError("R2 is not configured")
    return media_storage


def _parse_keep(raw):
    try:
        keep = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise RuntimeError("BACKUP_KEEP_COUNT is not an integer") from exc
    if keep < 1:
        raise RuntimeError("BACKUP_KEEP_COUNT must be >= 1")
    return keep


def _run_dump(cmd, destination, env):
    log.info("dump command: %s", " ".join(redact_command(cmd)))
    with destination.open("wb") as stdout:
        completed = subprocess.run(
            cmd,
            stdout=stdout,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"pg_dump exited {completed.returncode}: {detail[:2000]}")
    size = destination.stat().st_size
    if size <= 0:
        raise RuntimeError("pg_dump wrote an empty file")
    log.info("dump bytes=%s", size)
    return size


def _gzip_file(source, destination):
    with source.open("rb") as raw, gzip.open(destination, "wb") as packed:
        shutil.copyfileobj(raw, packed, length=1024 * 1024)
    size = destination.stat().st_size
    if size <= 0:
        raise RuntimeError("gzip wrote an empty file")
    return size


def _list_backup_keys(storage):
    client = storage._client()
    bucket = storage._bucket()
    keys = []
    token = None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": BACKUP_PREFIX}
        if token:
            kwargs["ContinuationToken"] = token
        page = client.list_objects_v2(**kwargs)
        for item in page.get("Contents") or []:
            keys.append(item.get("Key") or "")
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")
        if not token:
            break
    return keys


def _delete_backup_key(storage, key):
    if not is_backup_object_key(key) or is_protected_key(key):
        raise RuntimeError("refusing to delete a key that is not a backup object")
    storage.delete_object(key)
    log.info("deleted key=%s", key)


def _prepare_env():
    env_file = os.environ.get("BACKUP_ENV_FILE", "").strip()
    path = Path(env_file) if env_file else ROOT / "deploy" / ".env"
    if path.is_file():
        load_env_file(path, os.environ)
        log.info("loaded env file name=%s", path.name)
    elif env_file:
        raise RuntimeError("BACKUP_ENV_FILE does not exist")


def run_backup():
    _prepare_env()
    mode = os.environ.get("BACKUP_DUMP_MODE", "compose").strip().lower()
    keep = _parse_keep(os.environ.get("BACKUP_KEEP_COUNT", str(DEFAULT_KEEP)))
    storage = _storage()
    key = backup_object_key()
    if not is_backup_object_key(key):
        raise RuntimeError("refusing to upload because the new object key failed validation")

    compose_file = Path(os.environ.get("COMPOSE_FILE", ROOT / "deploy" / "docker-compose.yml"))
    if mode == "compose":
        docker_bin = _which("docker", ("/usr/bin/docker", "/usr/local/bin/docker"))
        pg_dump_bin = "pg_dump"
    elif mode == "pg_dump":
        docker_bin = "docker"
        pg_dump_bin = _which("pg_dump", ("/usr/bin/pg_dump", "/usr/lib/postgresql/16/bin/pg_dump"))
    else:
        raise RuntimeError("BACKUP_DUMP_MODE must be compose or pg_dump")
    log.info("start mode=%s keep=%s", mode, keep)
    command = build_dump_command(
        mode,
        user=os.environ.get("POSTGRES_USER", ""),
        database=os.environ.get("POSTGRES_DB", ""),
        database_url=os.environ.get("DATABASE_URL", ""),
        compose_file=compose_file,
        docker_bin=docker_bin,
        pg_dump_bin=pg_dump_bin,
    )
    child_env = os.environ.copy()
    child_env["PGCLIENTENCODING"] = "UTF8"
    tmp_root = os.environ.get("BACKUP_TMPDIR", "").strip() or None

    with tempfile.TemporaryDirectory(prefix="gojo-backup-", dir=tmp_root) as tmp:
        sql_path = Path(tmp) / "dump.sql"
        gz_path = Path(tmp) / "dump.sql.gz"
        _run_dump(command, sql_path, child_env)
        packed = _gzip_file(sql_path, gz_path)
        sql_path.unlink()
        payload = gz_path.read_bytes()
        if len(payload) != packed:
            raise RuntimeError("gzip file changed size before upload")
        storage.put_bytes(key, payload, "application/gzip")
        log.info("uploaded key=%s bytes=%s", key, packed)

    try:
        listed = _list_backup_keys(storage)
        delete_keys, ignored = retention_plan(listed, keep, key)
    except Exception:
        log.error("upload succeeded but retention did not start key=%s", key)
        raise
    for ignored_key in ignored:
        log.info("retention skipped key=%s", ignored_key)
    log.info("retention listed=%s delete=%s keep=%s", len(listed), len(delete_keys), keep)
    deleted = 0
    try:
        for old in delete_keys:
            _delete_backup_key(storage, old)
            deleted += 1
    except Exception:
        log.error(
            "upload succeeded but retention stopped key=%s deleted=%s planned=%s",
            key, deleted, len(delete_keys),
        )
        raise
    log.info("done key=%s", key)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        run_backup()
    except Exception:
        log.exception("[backup] failed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
