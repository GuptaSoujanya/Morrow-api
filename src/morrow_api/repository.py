import hashlib
import json
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import HTTPException
from psycopg.types.json import Jsonb


def audit(conn, actor_id, tenant_id, action: str, resource: str, reason: str):
    conn.execute("INSERT INTO audit_events(actor_id,target_tenant_id,action,resource,reason) VALUES (%s,%s,%s,%s,%s)", (actor_id, tenant_id, action, resource, reason))


def profile(conn, tenant_id):
    row = conn.execute("SELECT version,payload AS data,updated_at FROM profiles WHERE tenant_id=%s", (tenant_id,)).fetchone()
    return row or {"version": 0, "data": {}, "updated_at": None}


def save_profile(conn, tenant_id, expected_version: int, data: dict):
    if expected_version == 0:
        row = conn.execute("INSERT INTO profiles(tenant_id,version,payload) VALUES (%s,1,%s) ON CONFLICT DO NOTHING RETURNING version,payload AS data,updated_at", (tenant_id, Jsonb(data))).fetchone()
    else:
        row = conn.execute("UPDATE profiles SET version=version+1,payload=%s,updated_at=now() WHERE tenant_id=%s AND version=%s RETURNING version,payload AS data,updated_at", (Jsonb(data), tenant_id, expected_version)).fetchone()
    if not row:
        raise HTTPException(409, "Profile changed. Reload its current version before saving.")
    conn.execute("INSERT INTO profile_versions(tenant_id,version,payload) VALUES (%s,%s,%s)", (tenant_id, row["version"], Jsonb(data)))
    return row


def save_application(conn, tenant_id: UUID, key: str, data: dict):
    digest = hashlib.sha256(json.dumps(data, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()
    record = dict(data)
    record["occurred_at"] = record["occurred_at"] or datetime.now(timezone.utc)
    row = conn.execute(
        """INSERT INTO applications(id,tenant_id,idempotency_key,request_hash,platform,external_job_id,title,company,url,status,detail,occurred_at,metadata)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (tenant_id,idempotency_key) DO NOTHING RETURNING *""",
        (uuid4(), tenant_id, key, digest, record["platform"], record["external_job_id"], record["title"], record["company"], record["url"], record["status"], record["detail"], record["occurred_at"], Jsonb(record["metadata"])),
    ).fetchone()
    if not row:
        row = conn.execute("SELECT * FROM applications WHERE tenant_id=%s AND idempotency_key=%s", (tenant_id, key)).fetchone()
        if row["request_hash"] != digest:
            raise HTTPException(409, "Idempotency key was already used for different data")
    return public_application(row)


def public_application(row):
    return {key: value for key, value in row.items() if key not in ("tenant_id", "idempotency_key", "request_hash")}


def applications(conn, tenant_id, limit, offset):
    return [public_application(row) for row in conn.execute("SELECT * FROM applications WHERE tenant_id=%s ORDER BY occurred_at DESC,id DESC LIMIT %s OFFSET %s", (tenant_id, limit, offset)).fetchall()]


def save_record(conn, tenant_id, collection, key, data):
    return conn.execute("""INSERT INTO workspace_records(id,tenant_id,collection,record_key,payload) VALUES (%s,%s,%s,%s,%s)
        ON CONFLICT (tenant_id,collection,record_key) DO UPDATE SET payload=excluded.payload,updated_at=now()
        RETURNING id,collection,record_key AS key,payload AS data,updated_at""", (uuid4(), tenant_id, collection, key, Jsonb(data))).fetchone()


def records(conn, tenant_id, collection, limit, offset):
    return conn.execute("SELECT id,collection,record_key AS key,payload AS data,updated_at FROM workspace_records WHERE tenant_id=%s AND (%s::text IS NULL OR collection=%s) ORDER BY collection,record_key LIMIT %s OFFSET %s", (tenant_id, collection, collection, limit, offset)).fetchall()


ARTIFACT_COLUMNS = "id,filename,media_type,category,sha256,size_bytes,created_at"


def artifacts(conn, tenant_id, limit, offset):
    return conn.execute(f"SELECT {ARTIFACT_COLUMNS} FROM artifacts WHERE tenant_id=%s AND deleted_at IS NULL ORDER BY created_at DESC,id DESC LIMIT %s OFFSET %s", (tenant_id, limit, offset)).fetchall()


def artifact(conn, tenant_id, artifact_id):
    row = conn.execute("SELECT * FROM artifacts WHERE tenant_id=%s AND id=%s AND deleted_at IS NULL", (tenant_id, artifact_id)).fetchone()
    if not row:
        raise HTTPException(404, "Artifact not found")
    return row


def save_artifact(conn, storage, tenant_id, filename, media_type, category, body, source_key=None):
    if source_key:
        existing = conn.execute(f"SELECT {ARTIFACT_COLUMNS} FROM artifacts WHERE tenant_id=%s AND source_key=%s AND deleted_at IS NULL", (tenant_id, source_key)).fetchone()
        if existing:
            return existing
    identifier = uuid4()
    key = f"tenants/{tenant_id}/artifacts/{identifier}"
    digest = hashlib.sha256(body).hexdigest()
    storage.put(key, body)
    try:
        return conn.execute(f"""INSERT INTO artifacts(id,tenant_id,filename,media_type,category,object_key,sha256,size_bytes,source_key)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING {ARTIFACT_COLUMNS}""", (identifier, tenant_id, filename, media_type, category, key, digest, len(body), source_key)).fetchone()
    except Exception:
        storage.delete(key)
        raise
