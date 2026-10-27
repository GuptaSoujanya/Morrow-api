"""Import a Morrow user repo (the clone under user/) into one explicitly selected owner."""

import hashlib
import json
import mimetypes
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import psycopg
import yaml
from psycopg.rows import dict_row

from . import repository as repo
from .db import set_tenant
from .schemas import ApplicationCreate
from .storage import storage_for

TABLE_KEYS = {
    "jobs_seen": "job_id",
    "applications": "id",
    "answers": "question_norm",
    "runs": "id",
    "early_access_shares": "role_key",
}


def source_files(root):
    candidates = []
    for relative in (
        "inputs/profile.yaml",
        "inputs/answers.txt",
        "inputs/company_sites.md",
        "memory/progress.json",
        "memory/learning.json",
        "memory/profiles.json",
    ):
        path = root / relative
        if path.is_file():
            candidates.append(path)
    for folder, patterns in (
        ("research", ("*.md",)),
        ("resume", ("*.pdf", "*.tex")),
        ("reports", ("report.md", "*.eml")),
    ):
        for pattern in patterns:
            candidates.extend((root / folder).rglob(pattern))
    return sorted(
        {
            p
            for p in candidates
            if p.is_file()
            and not p.is_symlink()
            and p.resolve().is_relative_to(root)
            and ".git" not in p.parts
            and "backups" not in p.parts
        }
    )


def import_workspace(root: Path, email: str, apply: bool, settings, owner_url: str):
    root = root.resolve()
    if not root.is_dir():
        raise ValueError("Source directory does not exist")
    tables = {}
    db_path = root / "db/naukri_bot.db"
    if db_path.is_file():
        # SQLite backup creates a consistent snapshot while leaving the source read-only.
        with (
            sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True) as source,
            sqlite3.connect(":memory:") as snapshot,
        ):
            source.backup(snapshot)
            snapshot.row_factory = sqlite3.Row
            available = {
                row[0]
                for row in snapshot.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            for name in TABLE_KEYS:
                if name in available:
                    tables[name] = [
                        dict(row) for row in snapshot.execute(f'SELECT * FROM "{name}"')
                    ]
    paths = source_files(root)
    counts = {
        "mode": "apply" if apply else "preview",
        "tables": {name: len(rows) for name, rows in tables.items()},
        "artifact_files": len(paths),
        "oversized_files": sum(p.stat().st_size > settings.max_artifact_bytes for p in paths),
    }
    if not apply:
        return counts
    storage = storage_for(settings)
    storage.check()
    with psycopg.connect(owner_url, row_factory=dict_row) as conn:
        owner = conn.execute(
            "SELECT id,tenant_id FROM users WHERE email=%s AND active", (email.lower(),)
        ).fetchone()
        if not owner:
            raise ValueError("Create the destination owner before importing")
        set_tenant(conn, owner["tenant_id"])
        profile_path = root / "inputs/profile.yaml"
        if profile_path.is_file():
            data = yaml.safe_load(profile_path.read_text())
            if not isinstance(data, dict):
                raise ValueError("Legacy profile must contain a YAML mapping")
            # Normalize dates into JSON strings; YAML permits date-valued fields.
            data = json.loads(json.dumps(data, default=str))
            existing = repo.profile(conn, owner["tenant_id"])
            if existing["data"] != data:
                repo.save_profile(conn, owner["tenant_id"], existing["version"], data)
        for name, rows in tables.items():
            for row in rows:
                key = hashlib.sha256(str(row[TABLE_KEYS[name]]).encode()).hexdigest()
                repo.save_record(conn, owner["tenant_id"], "legacy_" + name, key, row)
                if name == "applications":
                    stamp = (
                        datetime.fromisoformat(row["at"]).replace(tzinfo=timezone.utc)
                        if row.get("at") and datetime.fromisoformat(row["at"]).tzinfo is None
                        else datetime.fromisoformat(row["at"])
                        if row.get("at")
                        else None
                    )
                    data = ApplicationCreate(
                        platform="legacy",
                        external_job_id=str(row.get("job_id") or "unknown"),
                        title=row.get("title") or "",
                        company=row.get("company") or "",
                        url=row.get("url") or "",
                        status=row.get("status") or "unknown",
                        detail=row.get("detail") or "",
                        occurred_at=stamp,
                        metadata={"legacy": row},
                    )
                    repo.save_application(
                        conn,
                        owner["tenant_id"],
                        "legacy-application-" + str(row["id"]),
                        data.model_dump(mode="json"),
                    )
        for path in paths:
            if path.stat().st_size > settings.max_artifact_bytes:
                continue
            body = path.read_bytes()
            if len(body) > settings.max_artifact_bytes:
                continue
            relative = path.relative_to(root).as_posix()
            key = relative + ":" + hashlib.sha256(body).hexdigest()
            repo.save_artifact(
                conn,
                storage,
                owner["tenant_id"],
                path.name,
                mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                "legacy",
                body,
                key,
            )
            if relative in (
                "memory/progress.json",
                "memory/learning.json",
                "memory/profiles.json",
            ):
                repo.save_record(
                    conn, owner["tenant_id"], "legacy_memory", path.stem, json.loads(body)
                )
        repo.audit(
            conn,
            owner["id"],
            owner["tenant_id"],
            "workspace.import",
            "legacy workspace",
            "Explicit local import into the named owner; credentials and browser sessions excluded",
        )
    return counts
