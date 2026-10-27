"""Write one owner's account data into a Morrow user repo layout (the mirror of importer).

The target is a clone of a private Morrow-user repo (user/ in a Morrow checkout):
inputs/profile.yaml gets the account profile merged over whatever is already in the
file, artifacts imported from a repo go back to their original relative paths,
uploaded resumes go to resume/, other uploads to inputs/account-files/<category>/,
and the memory records go to memory/<name>.json. Credentials and browser sessions
are never stored in an account, so they are never written.
"""

import hashlib
import json
from pathlib import Path

import psycopg
import yaml
from psycopg.rows import dict_row

from . import repository as repo
from .db import set_tenant
from .storage import storage_for


def _safe_relative(value: str):
    path = Path(value)
    if not value or path.is_absolute() or any(part in ("..", "", ".git") for part in path.parts):
        return None
    return path


def target_path(row) -> Path:
    source = row.get("source_key") or ""
    if row["category"] == "legacy" and ":" in source:
        relative = _safe_relative(source.rsplit(":", 1)[0])
        if relative is not None:
            return relative
    name = Path(row["filename"]).name or "file"
    if row["category"] == "resumes":
        return Path("resume") / name
    return Path("inputs/account-files") / Path(row["category"]).name / name


def _write(path: Path, body: bytes):
    if path.is_symlink():
        raise ValueError(f"{path} is a symbolic link; replace it with a regular file first")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".export")
    temporary.write_bytes(body)
    temporary.replace(path)


def export_workspace(email: str, target: Path, apply: bool, settings, owner_url: str):
    target = Path(target).resolve()
    counts = {
        "mode": "apply" if apply else "preview",
        "profile": False,
        "artifacts": 0,
        "unchanged": 0,
        "memory_records": 0,
        "files": [],
    }
    with psycopg.connect(owner_url, row_factory=dict_row) as conn:
        owner = conn.execute(
            "SELECT id,tenant_id FROM users WHERE email=%s AND active", (email.lower(),)
        ).fetchone()
        if not owner:
            raise ValueError("No active owner with that email; create the account first")
        tenant = owner["tenant_id"]
        set_tenant(conn, tenant)
        profile = repo.profile(conn, tenant)["data"]
        if profile:
            counts["profile"] = True
            path = target / "inputs/profile.yaml"
            existing = {}
            if path.is_file():
                loaded = yaml.safe_load(path.read_text())
                if loaded is not None and not isinstance(loaded, dict):
                    raise ValueError("inputs/profile.yaml must contain a YAML mapping")
                existing = loaded or {}
            merged = {**existing, **{k: v for k, v in profile.items() if not str(k).startswith("_")}}
            if merged != existing or not path.exists():
                counts["files"].append("inputs/profile.yaml")
                if apply:
                    _write(path, yaml.safe_dump(merged, allow_unicode=True, sort_keys=False).encode())
        # Memory records only fill in files that are missing; an imported memory/*.json
        # artifact below restores the exact original bytes and takes precedence.
        for record in repo.records(conn, tenant, "legacy_memory", 1000, 0):
            name = _safe_relative(str(record["key"]))
            if name is None or len(name.parts) != 1:
                continue
            path = target / "memory" / (name.name + ".json")
            if path.exists():
                continue
            counts["memory_records"] += 1
            counts["files"].append(path.relative_to(target).as_posix())
            if apply:
                _write(path, json.dumps(record["data"], indent=2).encode())
        rows = conn.execute(
            "SELECT id,filename,media_type,category,sha256,size_bytes,object_key,source_key FROM artifacts"
            " WHERE tenant_id=%s AND deleted_at IS NULL ORDER BY created_at,id",
            (tenant,),
        ).fetchall()
        storage = storage_for(settings) if apply and rows else None
        for row in rows:
            relative = target_path(row)
            if relative == Path("inputs/profile.yaml"):
                # The structured account profile above is the current version of this file.
                continue
            path = target / relative
            if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == row["sha256"]:
                counts["unchanged"] += 1
                continue
            counts["artifacts"] += 1
            counts["files"].append(relative.as_posix())
            if apply:
                body = storage.get(row["object_key"])
                if hashlib.sha256(body).hexdigest() != row["sha256"]:
                    raise ValueError(f"Stored file {row['filename']} does not match its recorded checksum")
                _write(path, body)
        if apply:
            repo.audit(
                conn,
                owner["id"],
                tenant,
                "workspace.export",
                str(target),
                "Explicit export of the owner's account data into a user repo clone",
            )
    counts["files"] = sorted(set(counts["files"]))
    return counts
