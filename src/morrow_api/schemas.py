from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(StrictModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=1024)


class ProfileUpdate(StrictModel):
    expected_version: int = Field(ge=0)
    data: dict[str, Any]


class RecordUpdate(StrictModel):
    data: dict[str, Any]


class ApplicationCreate(StrictModel):
    platform: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_-]+$")
    external_job_id: str = Field(min_length=1, max_length=256)
    title: str = Field(default="", max_length=512)
    company: str = Field(default="", max_length=512)
    url: str = Field(default="", max_length=4096)
    status: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_-]+$")
    detail: str = Field(default="", max_length=10000)
    occurred_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("occurred_at")
    @classmethod
    def aware_time(cls, value):
        if value is not None:
            if value.tzinfo is None:
                raise ValueError("occurred_at must include a timezone")
            return value.astimezone(timezone.utc)
        return value

    @field_validator("url")
    @classmethod
    def web_url(cls, value):
        if value and not value.startswith(("https://", "http://")):
            raise ValueError("url must use HTTP or HTTPS")
        return value
