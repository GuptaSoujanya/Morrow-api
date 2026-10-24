from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

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
    public_origin: str = "http://127.0.0.1:8100"
    # More browser origins that may call the API, e.g. localhost beside 127.0.0.1.
    extra_origins: list[str] = []
    session_secret: SecretStr = SecretStr("")
    cookie_secure: bool = False
    auth_provider: Literal["local", "firebase"] = "local"
    auth_ip_attempt_limit: int = Field(default=1000, ge=100, le=10000)
    firebase_project_id: str = ""
    firebase_api_key: str = ""
    firebase_auth_domain: str = ""
    firebase_app_id: str = ""
    google_client_id: str = ""
    google_client_secret: SecretStr = SecretStr("")
    mail_backend: Literal["file", "smtp", "resend"] = "file"
    mail_root: Path = Path(".runtime/mail")
    mail_from: str = "Morrow <hello@localhost>"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    resend_api_key: SecretStr = SecretStr("")
    stripe_secret_key: SecretStr = SecretStr("")
    stripe_webhook_secret: SecretStr = SecretStr("")

    @property
    def trusted_origins(self) -> set[str]:
        return {self.public_origin, *self.extra_origins}

    @field_validator("extra_origins")
    @classmethod
    def extra_dashboard_origins(cls, value: list[str]) -> list[str]:
        return [cls.dashboard_origin(origin) for origin in value]

    @field_validator("public_origin")
    @classmethod
    def dashboard_origin(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("PUBLIC_ORIGIN must be the dashboard HTTP(S) origin without a path")
        return value.rstrip("/")

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
