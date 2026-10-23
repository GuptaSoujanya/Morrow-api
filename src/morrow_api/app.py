import hashlib
import re
from contextlib import asynccontextmanager
from typing import Annotated
from urllib.parse import quote
from uuid import UUID

import psycopg
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import repository as repo
from .config import Settings
from .db import assert_runtime_role, pool_for, set_tenant, tenant_transaction
from .middleware import RequestBoundary
from .schemas import ApplicationCreate, Login, ProfileUpdate, RecordUpdate
from .security import administrator, current_user, login
from .storage import StorageError, storage_for

PageLimit = Annotated[int, Query(ge=1, le=100)]
PageOffset = Annotated[int, Query(ge=0, le=100000)]
Collection = Annotated[str, Query(min_length=1, max_length=64, pattern=r"^[a-z0-9_-]+$")]
Reason = Annotated[str, Header(alias="X-Admin-Reason", min_length=10, max_length=300, pattern=r"^[^\x00-\x1f]+$")]


def create_app(settings: Settings | None = None):
    settings = settings or Settings()
    pool = pool_for(settings.database_url.get_secret_value())
    storage = storage_for(settings)

    @asynccontextmanager
    async def lifespan(app):
        pool.open(wait=True)
        try:
            assert_runtime_role(pool)
            storage.check()
            yield
        finally:
            pool.close()

    app = FastAPI(title="Morrow API", version="0.1.0", lifespan=lifespan)
    app.state.pool, app.state.storage, app.state.settings = pool, storage, settings
    app.add_middleware(RequestBoundary, artifact_limit=settings.max_artifact_bytes)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        # FastAPI's default validation detail can echo password and profile inputs.
        errors = [{"loc": error["loc"], "msg": error["msg"], "type": error["type"]} for error in exc.errors()]
        return JSONResponse({"detail": errors}, status_code=422)

    @app.exception_handler(psycopg.Error)
    async def database_error(request, exc):
        return JSONResponse({"detail": "Storage temporarily unavailable"}, status_code=503)

    async def artifact_error(request, exc):
        return JSONResponse({"detail": "Artifact storage temporarily unavailable"}, status_code=503)

    for error_class in (StorageError, OSError, BotoCoreError, ClientError):
        app.add_exception_handler(error_class, artifact_error)

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready():
        with pool.connection() as conn:
            conn.execute("SELECT 1")
        return {"status": "ready"}

    @app.post("/api/v1/auth/login")
    def sign_in(data: Login, request: Request):
        return login(pool, settings, str(data.email).lower(), data.password, request.client.host if request.client else "unknown")

    @app.post("/api/v1/auth/logout", status_code=204)
    def sign_out(request: Request, user=Depends(current_user)):
        with pool.connection() as conn:
            conn.execute("DELETE FROM auth_sessions WHERE token_hash=%s", (request.state.token_hash,))
        return Response(status_code=204)

    @app.get("/api/v1/me")
    def me(user=Depends(current_user)):
        return user

    @app.get("/api/v1/profile")
    def get_profile(user=Depends(current_user)):
        with tenant_transaction(pool, user["tenant_id"]) as conn:
            return repo.profile(conn, user["tenant_id"])

    @app.put("/api/v1/profile")
    def put_profile(data: ProfileUpdate, user=Depends(current_user)):
        with tenant_transaction(pool, user["tenant_id"]) as conn:
            return repo.save_profile(conn, user["tenant_id"], data.expected_version, data.data)

    @app.get("/api/v1/applications")
    def list_applications(limit: PageLimit = 50, offset: PageOffset = 0, user=Depends(current_user)):
        with tenant_transaction(pool, user["tenant_id"]) as conn:
            return {"items": repo.applications(conn, user["tenant_id"], limit, offset), "limit": limit, "offset": offset}

    @app.get("/api/v1/applications/{application_id}")
    def get_application(application_id: UUID, user=Depends(current_user)):
        with tenant_transaction(pool, user["tenant_id"]) as conn:
            row = conn.execute("SELECT * FROM applications WHERE tenant_id=%s AND id=%s", (user["tenant_id"], application_id)).fetchone()
            if not row:
                raise HTTPException(404, "Application not found")
            return repo.public_application(row)

    @app.post("/api/v1/applications", status_code=201)
    def put_application(data: ApplicationCreate, idempotency_key: Annotated[str, Header(min_length=1, max_length=128)], user=Depends(current_user)):
        with tenant_transaction(pool, user["tenant_id"]) as conn:
            return repo.save_application(conn, user["tenant_id"], idempotency_key, data.model_dump(mode="json"))

    @app.get("/api/v1/records")
    def list_records(collection: Collection | None = None, limit: PageLimit = 50, offset: PageOffset = 0, user=Depends(current_user)):
        with tenant_transaction(pool, user["tenant_id"]) as conn:
            return {"items": repo.records(conn, user["tenant_id"], collection, limit, offset), "limit": limit, "offset": offset}

    @app.put("/api/v1/records/{collection}/{key}")
    def put_record(collection: str, key: str, data: RecordUpdate, user=Depends(current_user)):
        if not re.fullmatch(r"[a-z0-9_-]{1,64}", collection) or not 1 <= len(key) <= 256:
            raise HTTPException(422, "Invalid collection or record key")
        with tenant_transaction(pool, user["tenant_id"]) as conn:
            return repo.save_record(conn, user["tenant_id"], collection, key, data.data)

    @app.get("/api/v1/artifacts")
    def list_artifacts(limit: PageLimit = 50, offset: PageOffset = 0, user=Depends(current_user)):
        with tenant_transaction(pool, user["tenant_id"]) as conn:
            return {"items": repo.artifacts(conn, user["tenant_id"], limit, offset), "limit": limit, "offset": offset}

    @app.post("/api/v1/artifacts", status_code=201)
    def upload_artifact(request: Request, filename: Annotated[str, Query(min_length=1, max_length=200)], category: Collection = "document", body: bytes = Body(media_type="application/octet-stream"), user=Depends(current_user)):
        if re.search(r"[\x00-\x1f\x7f/\\]", filename) or filename in (".", ".."):
            raise HTTPException(422, "Invalid filename")
        content_type = request.headers.get("content-type", "application/octet-stream").split(";")[0]
        if len(content_type) > 120:
            raise HTTPException(422, "Invalid content type")
        with tenant_transaction(pool, user["tenant_id"]) as conn:
            return repo.save_artifact(conn, storage, user["tenant_id"], filename, content_type, category, body)

    def download(row):
        body = storage.get(row["object_key"])
        if hashlib.sha256(body).hexdigest() != row["sha256"]:
            raise StorageError("Artifact checksum mismatch")
        return Response(body, media_type="application/octet-stream", headers={"Content-Disposition": "attachment; filename=download; filename*=UTF-8''" + quote(row["filename"], safe="")})

    @app.get("/api/v1/artifacts/{artifact_id}/content")
    def download_artifact(artifact_id: UUID, user=Depends(current_user)):
        with tenant_transaction(pool, user["tenant_id"]) as conn:
            row = repo.artifact(conn, user["tenant_id"], artifact_id)
        return download(row)

    @app.delete("/api/v1/artifacts/{artifact_id}", status_code=204)
    def delete_artifact(artifact_id: UUID, user=Depends(current_user)):
        with tenant_transaction(pool, user["tenant_id"]) as conn:
            row = repo.artifact(conn, user["tenant_id"], artifact_id)
            conn.execute("UPDATE artifacts SET deleted_at=now() WHERE id=%s AND tenant_id=%s", (artifact_id, user["tenant_id"]))
        # A failed deletion leaves an inaccessible tombstone; CLI purge can retry it.
        storage.delete(row["object_key"])
        return Response(status_code=204)

    def admin_target(conn, user, target_id, action, resource, reason):
        target = conn.execute("SELECT tenant_id FROM users WHERE id=%s", (target_id,)).fetchone()
        if not target:
            raise HTTPException(404, "User not found")
        set_tenant(conn, target["tenant_id"])
        repo.audit(conn, user["id"], target["tenant_id"], action, resource, reason)
        return target["tenant_id"]

    @app.get("/api/v1/admin/users")
    def admin_users(reason: Reason, limit: PageLimit = 50, offset: PageOffset = 0, user=Depends(administrator)):
        with pool.connection() as conn, conn.transaction():
            repo.audit(conn, user["id"], None, "admin.users.list", "users", reason)
            rows = conn.execute("SELECT id,email,role,active,created_at FROM users ORDER BY created_at,id LIMIT %s OFFSET %s", (limit, offset)).fetchall()
        return {"items": rows, "limit": limit, "offset": offset}

    @app.get("/api/v1/admin/users/{user_id}/profile")
    def admin_profile(user_id: UUID, reason: Reason, user=Depends(administrator)):
        with pool.connection() as conn, conn.transaction():
            tenant = admin_target(conn, user, user_id, "admin.profile.read", "profile", reason)
            return repo.profile(conn, tenant)

    @app.get("/api/v1/admin/users/{user_id}/applications")
    def admin_applications(user_id: UUID, reason: Reason, limit: PageLimit = 50, offset: PageOffset = 0, user=Depends(administrator)):
        with pool.connection() as conn, conn.transaction():
            tenant = admin_target(conn, user, user_id, "admin.applications.list", "applications", reason)
            return {"items": repo.applications(conn, tenant, limit, offset), "limit": limit, "offset": offset}

    @app.get("/api/v1/admin/users/{user_id}/records")
    def admin_records(user_id: UUID, reason: Reason, collection: Collection | None = None, limit: PageLimit = 50, offset: PageOffset = 0, user=Depends(administrator)):
        with pool.connection() as conn, conn.transaction():
            tenant = admin_target(conn, user, user_id, "admin.records.list", collection or "records", reason)
            return {"items": repo.records(conn, tenant, collection, limit, offset), "limit": limit, "offset": offset}

    @app.get("/api/v1/admin/users/{user_id}/artifacts")
    def admin_artifacts(user_id: UUID, reason: Reason, limit: PageLimit = 50, offset: PageOffset = 0, user=Depends(administrator)):
        with pool.connection() as conn, conn.transaction():
            tenant = admin_target(conn, user, user_id, "admin.artifacts.list", "artifacts", reason)
            return {"items": repo.artifacts(conn, tenant, limit, offset), "limit": limit, "offset": offset}

    @app.get("/api/v1/admin/users/{user_id}/artifacts/{artifact_id}/content")
    def admin_download(user_id: UUID, artifact_id: UUID, reason: Reason, user=Depends(administrator)):
        with pool.connection() as conn, conn.transaction():
            tenant = admin_target(conn, user, user_id, "admin.artifact.download", str(artifact_id), reason)
            row = repo.artifact(conn, tenant, artifact_id)
        # Audit commits before any private bytes leave the service.
        return download(row)

    @app.get("/api/v1/admin/audit-events")
    def admin_audit(reason: Reason, limit: PageLimit = 50, offset: PageOffset = 0, user=Depends(administrator)):
        with pool.connection() as conn, conn.transaction():
            repo.audit(conn, user["id"], None, "admin.audit.list", "audit_events", reason)
            rows = conn.execute("SELECT * FROM audit_events ORDER BY id DESC LIMIT %s OFFSET %s", (limit, offset)).fetchall()
        return {"items": rows, "limit": limit, "offset": offset}

    return app
