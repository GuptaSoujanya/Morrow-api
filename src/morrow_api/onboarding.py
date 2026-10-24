"""Private setup preferences. Browser-session verification belongs to the local runner."""

import hashlib
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from psycopg.types.json import Jsonb
from pydantic import EmailStr, Field, TypeAdapter, ValidationError, field_validator

from . import repository as repo
from .db import tenant_transaction
from .schemas import StrictModel
from .security import current_user
from .storage import StorageError

Provider = Literal["naukri", "linkedin", "hirist", "instahyre", "cutshort", "wellfound", "google"]
AccountHint = Annotated[str, Field(max_length=254, pattern=r"^[^\x00-\x1f\x7f]*$")]
EMAIL = TypeAdapter(EmailStr)
PROFILE_FIELDS = ("full_name", "email", "phone", "current_location", "titles")


class OnboardingUpdate(StrictModel):
    expected_version: int = Field(ge=0)
    platforms: list[Provider] = Field(max_length=7)
    resume_artifact_id: UUID | None = None
    account_hints: dict[Provider, AccountHint] = Field(default_factory=dict)
    completed: bool = Field(default=False, strict=True)

    @field_validator("platforms")
    @classmethod
    def unique_platforms(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("Choose each platform only once")
        return value

    @field_validator("account_hints")
    @classmethod
    def tidy_hints(cls, value):
        return {provider: hint.strip() for provider, hint in value.items() if hint.strip()}


def missing_profile_fields(data):
    missing = []
    for name in PROFILE_FIELDS:
        value = data.get(name)
        if name == "titles":
            valid = (
                isinstance(value, list)
                and bool(value)
                and all(isinstance(item, str) and item.strip() for item in value)
            )
        else:
            valid = isinstance(value, str) and bool(value.strip())
        if valid and name == "email":
            try:
                EMAIL.validate_python(value)
            except ValidationError:
                valid = False
        if not valid:
            missing.append(name)
    return missing


def resume_metadata(conn, tenant_id, artifact_id, *, lock=False):
    if artifact_id is None:
        return None
    return conn.execute(
        "SELECT * FROM artifacts WHERE tenant_id=%s AND id=%s AND deleted_at IS NULL"
        + (" FOR SHARE" if lock else ""),
        (tenant_id, artifact_id),
    ).fetchone()


def valid_resume_metadata(row):
    return bool(
        row
        and row["category"] == "resumes"
        and row["filename"].lower().endswith(".pdf")
        and row["media_type"] in ("application/pdf", "application/octet-stream")
        and row["size_bytes"] > 0
    )


def public_state(conn, tenant_id, row=None):
    if row is None:
        row = conn.execute(
            "SELECT version,payload,completed_at FROM onboarding_state WHERE tenant_id=%s",
            (tenant_id,),
        ).fetchone()
    preferences = row["payload"] if row else {}
    profile = repo.profile(conn, tenant_id)
    artifact_id = preferences.get("resume_artifact_id")
    resume = resume_metadata(conn, tenant_id, artifact_id)
    resume_ready = valid_resume_metadata(resume)
    missing = missing_profile_fields(profile["data"])
    return {
        "version": row["version"] if row else 0,
        "platforms": preferences.get("platforms", []),
        "resume_artifact_id": artifact_id,
        "resume": {key: resume[key] for key in repo.ARTIFACT_COLUMNS.split(",")}
        if resume_ready
        else None,
        "profile": profile,
        "account_hints": preferences.get("account_hints", {}),
        "completed_at": row["completed_at"] if row else None,
        "readiness": {
            "profile_complete": not missing,
            "resume_ready": resume_ready,
            "missing_profile_fields": missing,
        },
    }


def onboarding_router(pool, storage):
    router = APIRouter(prefix="/api/v1/onboarding", tags=["onboarding"])

    @router.get("")
    def get_onboarding(user=Depends(current_user)):
        with tenant_transaction(pool, user["tenant_id"]) as conn:
            return public_state(conn, user["tenant_id"])

    @router.put("")
    def put_onboarding(data: OnboardingUpdate, user=Depends(current_user)):
        tenant_id = user["tenant_id"]
        with tenant_transaction(pool, tenant_id) as conn:
            if data.resume_artifact_id:
                resume = resume_metadata(conn, tenant_id, data.resume_artifact_id, lock=True)
                if not resume:
                    raise HTTPException(404, "Resume not found in your private workspace")
                if not valid_resume_metadata(resume):
                    raise HTTPException(422, "Choose a PDF uploaded to your private resumes")
                body = storage.get(resume["object_key"])
                if hashlib.sha256(body).hexdigest() != resume["sha256"]:
                    raise StorageError("Resume checksum mismatch")
                if not body.startswith(b"%PDF-"):
                    raise HTTPException(422, "The selected file is not a PDF")

            if data.completed:
                profile = repo.profile(conn, tenant_id)
                if missing_profile_fields(profile["data"]):
                    raise HTTPException(422, "Complete your profile before finishing setup")
                if not data.resume_artifact_id:
                    raise HTTPException(422, "Choose a resume before finishing setup")
                if not data.platforms:
                    raise HTTPException(422, "Choose at least one platform before finishing setup")

            payload = Jsonb(data.model_dump(mode="json", exclude={"expected_version", "completed"}))
            if data.expected_version == 0:
                row = conn.execute(
                    """INSERT INTO onboarding_state(tenant_id,version,payload,completed_at)
                    VALUES (%s,1,%s,CASE WHEN %s THEN now() END)
                    ON CONFLICT DO NOTHING RETURNING version,payload,completed_at""",
                    (tenant_id, payload, data.completed),
                ).fetchone()
            else:
                row = conn.execute(
                    """UPDATE onboarding_state SET version=version+1,payload=%s,
                    completed_at=CASE WHEN %s THEN coalesce(completed_at,now()) END,
                    updated_at=now() WHERE tenant_id=%s AND version=%s
                    RETURNING version,payload,completed_at""",
                    (payload, data.completed, tenant_id, data.expected_version),
                ).fetchone()
            if not row:
                raise HTTPException(409, "Setup changed. Reload its current version before saving.")
            return public_state(conn, tenant_id, row)

    return router
