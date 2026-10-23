from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: SecretStr
    artifact_backend: Literal["local", "s3"] = "local"
    artifact_root: Path = Path(".runtime/artifacts")
    max_artifact_bytes: int = Field(default=10 * 1024 * 1024, ge=1, le=100 * 1024 * 1024)
    session_hours: int = Field(default=12, ge=1, le=168)
    allowed_hosts: list[str] = ["localhost", "127.0.0.1", "testserver"]
    s3_bucket: str = ""
    s3_region: str = "us-east-1"
    s3_endpoint_url: str | None = None
    public_origin: str = "http://127.0.0.1:8200"
    session_secret: SecretStr = SecretStr("")
    cookie_secure: bool = False
    google_client_id: str = ""
    google_client_secret: SecretStr = SecretStr("")
    mail_backend: Literal["file", "smtp"] = "file"
    mail_root: Path = Path(".runtime/mail")
    mail_from: str = "Morrow <hello@localhost>"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    stripe_secret_key: SecretStr = SecretStr("")
    stripe_webhook_secret: SecretStr = SecretStr("")
    dashboard_url: str = "http://127.0.0.1:8100"

    @field_validator("database_url")
    @classmethod
    def postgres_only(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith(("postgresql://", "postgres://")):
            raise ValueError("DATABASE_URL must use PostgreSQL")
        return value

    @field_validator("s3_endpoint_url", mode="before")
    @classmethod
    def empty_endpoint(cls, value):
        if not value:
            return None
        if not value.startswith("https://"):
            raise ValueError("S3_ENDPOINT_URL must use HTTPS")
        return value
