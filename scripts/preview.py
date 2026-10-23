"""Start an isolated local preview with synthetic accounts. Never use as production setup."""

from pathlib import Path
from uuid import uuid4

import psycopg
import uvicorn
from dotenv import dotenv_values
from psycopg import sql
from psycopg.conninfo import make_conninfo

from morrow_api.app import create_app
from morrow_api.config import Settings
from morrow_api.migrate import migrate
from morrow_api.security import hasher

root = Path(__file__).resolve().parents[1]
env = dotenv_values(root / ".env")
owner = env["DATABASE_OWNER_URL"]
database = "morrow_preview"
with psycopg.connect(owner, autocommit=True) as conn:
    if not conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (database,)).fetchone():
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
preview_owner = make_conninfo(owner, dbname=database)
migrate(preview_owner, env["APP_DATABASE_PASSWORD"])
with psycopg.connect(preview_owner) as conn:
    # This database contains synthetic preview accounts only. A fresh preview
    # should not inherit login throttles from previous browser test runs.
    conn.execute("DELETE FROM auth_limits")
    for email, name, role in (
        ("preview.admin@example.com", "Alex Morgan", "admin"),
        ("preview.user@example.com", "Jamie Chen", "user"),
    ):
        conn.execute(
            "INSERT INTO users(id,tenant_id,email,name,password_hash,role,email_verified) VALUES (%s,%s,%s,%s,%s,%s,true) ON CONFLICT(email) DO NOTHING",
            (uuid4(), uuid4(), email, name, hasher.hash("Preview-only-passphrase-2026!"), role),
        )
settings = Settings(
    _env_file=None,
    database_url=env["DATABASE_URL"].rsplit("/", 1)[0] + "/" + database,
    session_secret=env["SESSION_SECRET"],
    public_origin="http://127.0.0.1:8102",
    artifact_root=root / ".runtime/preview/artifacts",
    mail_root=root / ".runtime/preview/mail",
    google_client_id="",
    google_client_secret="",
    stripe_secret_key="",
    stripe_webhook_secret="",
    artifact_backend="local",
    mail_backend="file",
)
print("Synthetic API: http://127.0.0.1:8201; dashboard origin: http://127.0.0.1:8102", flush=True)
uvicorn.run(create_app(settings), host="127.0.0.1", port=8201, access_log=False)
