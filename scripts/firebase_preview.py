"""Run real Firebase sign-in against a separate, empty Morrow workspace database.

Start the existing dashboard with MORROW_API_URL=http://127.0.0.1:8203 and
MORROW_WORKSPACE_OWNER_EMAIL='' on port 8103, then use http://localhost:8103.
The main API, its configuration, and imported workspace are left in place.
"""

import json
import os
import secrets
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
import uvicorn
from dotenv import dotenv_values
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from morrow_api.app import create_app
from morrow_api.config import Settings
from morrow_api.migrate import migrate

ROOT = Path(__file__).resolve().parents[1]
DATABASE = "morrow_firebase_preview"
ORIGIN = "http://localhost:8103"


def preview_settings():
    env = dotenv_values(ROOT / ".env")
    required = (
        "DATABASE_OWNER_URL",
        "DATABASE_URL",
        "APP_DATABASE_PASSWORD",
        "FIREBASE_PROJECT_ID",
        "FIREBASE_API_KEY",
        "FIREBASE_AUTH_DOMAIN",
        "FIREBASE_APP_ID",
    )
    missing = [key for key in required if not env.get(key)]
    if missing:
        raise ValueError("Missing preview configuration in .env: " + ", ".join(missing))
    if os.getenv("FIREBASE_AUTH_EMULATOR_HOST"):
        raise ValueError("Unset FIREBASE_AUTH_EMULATOR_HOST; this preview uses real Firebase.")

    credential_path = ROOT / ".runtime/firebase-admin.json"
    if not credential_path.is_file():
        raise ValueError(
            "Save the private server credential to .runtime/firebase-admin.json first."
        )
    if credential_path.stat().st_mode & 0o077:
        raise ValueError("The private Firebase credential must have permissions 0600.")
    try:
        credential = json.loads(credential_path.read_text())
    except (OSError, ValueError):
        raise ValueError(
            "The private Firebase credential is unreadable or is not valid JSON."
        ) from None
    if (
        not isinstance(credential, dict)
        or credential.get("type") != "service_account"
        or credential.get("project_id") != env["FIREBASE_PROJECT_ID"]
        or not credential.get("client_email")
        or not credential.get("private_key")
    ):
        raise ValueError("The credential must be a service-account key for the configured project.")
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(credential_path)

    owner = conninfo_to_dict(env["DATABASE_OWNER_URL"])
    app = conninfo_to_dict(env["DATABASE_URL"])
    if owner.get("dbname") == DATABASE or app.get("dbname") == DATABASE:
        raise ValueError("The main database settings must not already target the preview database.")
    if any(owner.get(key) != app.get(key) for key in ("host", "port", "dbname")):
        raise ValueError(
            "Main owner and application database settings must target the same server."
        )
    app_url = urlsplit(env["DATABASE_URL"])
    if app_url.scheme not in ("postgresql", "postgres"):
        raise ValueError("DATABASE_URL must be a PostgreSQL URL.")
    if (
        len(env["APP_DATABASE_PASSWORD"]) < 24
        or app.get("password") != env["APP_DATABASE_PASSWORD"]
    ):
        raise ValueError(
            "The existing application database password must match APP_DATABASE_PASSWORD."
        )

    # The marker prevents reuse of an unrelated database with the same name.
    marker = "morrow-firebase-preview:v1:" + env["FIREBASE_PROJECT_ID"]
    with psycopg.connect(env["DATABASE_OWNER_URL"], autocommit=True) as conn:
        existing = conn.execute(
            "SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname=%s",
            (DATABASE,),
        ).fetchone()
        if existing is not None and existing[0] != marker:
            raise ValueError("Refusing to use an unrecognized database named " + DATABASE + ".")
        if existing is None:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DATABASE)))
            conn.execute(
                sql.SQL("COMMENT ON DATABASE {} IS {}").format(
                    sql.Identifier(DATABASE), sql.Literal(marker)
                )
            )
    preview_owner = make_conninfo(env["DATABASE_OWNER_URL"], dbname=DATABASE)
    migrate(preview_owner, env["APP_DATABASE_PASSWORD"])

    return Settings(
        _env_file=None,
        database_url=urlunsplit(app_url._replace(path="/" + DATABASE)),
        session_secret=secrets.token_urlsafe(48),
        public_origin=ORIGIN,
        allowed_hosts=["localhost", "127.0.0.1"],
        cookie_secure=False,
        auth_provider="firebase",
        firebase_project_id=env["FIREBASE_PROJECT_ID"],
        firebase_api_key=env["FIREBASE_API_KEY"],
        firebase_auth_domain=env["FIREBASE_AUTH_DOMAIN"],
        firebase_app_id=env["FIREBASE_APP_ID"],
        artifact_root=ROOT / ".runtime/firebase-preview/artifacts",
        mail_root=ROOT / ".runtime/firebase-preview/mail",
        artifact_backend="local",
        mail_backend="file",
        google_client_id="",
        google_client_secret="",
        stripe_secret_key="",
        stripe_webhook_secret="",
        smtp_host="",
        smtp_username="",
        smtp_password="",
        resend_api_key="",
    )


def main():
    try:
        settings = preview_settings()
        app = create_app(settings)
    except (ValueError, OSError, psycopg.Error) as exc:
        # Database exceptions may contain connection credentials.
        detail = (
            str(exc) if isinstance(exc, ValueError) else "Check database and private-file access."
        )
        sys.exit("Firebase preview could not start: " + detail)
    print("Firebase preview API: http://127.0.0.1:8203; dashboard: " + ORIGIN, flush=True)
    print("This preview has a separate workspace database and no imported data.", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=8203, access_log=False)


if __name__ == "__main__":
    main()
