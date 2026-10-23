import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from dotenv import dotenv_values
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from morrow_api.app import create_app
from morrow_api.config import Settings
from morrow_api.migrate import migrate
from morrow_api.security import hasher

PASSWORD = "Test-only-passphrase-123!"


@pytest.fixture(scope="session")
def database():
    env = dotenv_values(Path(__file__).parents[1] / ".env")
    owner = os.getenv("TEST_DATABASE_OWNER_URL") or env.get("DATABASE_OWNER_URL")
    if not owner:
        pytest.fail("Set TEST_DATABASE_OWNER_URL or run morrow init-local and start PostgreSQL")
    # Always create a separate randomly named database. Never truncate the configured DB.
    name = "morrow_test_" + uuid4().hex[:12]
    password = env.get("APP_DATABASE_PASSWORD") or "isolated-test-db-password-123456789"
    try:
        owner_connection = psycopg.connect(owner, autocommit=True)
    except psycopg.Error:
        pytest.fail(
            "Cannot connect to the test database server; check local PostgreSQL access",
            pytrace=False,
        )
    with owner_connection as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    test_owner = make_conninfo(owner, dbname=name)
    migrate(test_owner, password)
    app_url = make_conninfo(owner, dbname=name, user="morrow_app", password=password)
    # Settings deliberately accept URL DSNs only.
    from urllib.parse import quote

    from psycopg.conninfo import conninfo_to_dict

    fields = conninfo_to_dict(app_url)
    app_url = f"postgresql://morrow_app:{quote(password)}@{fields['host']}:{fields.get('port', 5432)}/{name}"
    with psycopg.connect(test_owner, row_factory=dict_row) as conn:
        plans = conn.execute("SELECT * FROM plans").fetchall()
    yield {"owner": test_owner, "app": app_url, "plans": plans}
    with psycopg.connect(owner, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
def setup(database, tmp_path):
    with psycopg.connect(database["owner"], row_factory=dict_row) as conn:
        conn.execute(
            "TRUNCATE users,auth_sessions,auth_limits,profiles,profile_versions,applications,workspace_records,artifacts,audit_events,auth_tokens,oauth_states,mail_outbox,stripe_events RESTART IDENTITY CASCADE"
        )
        for plan in database["plans"]:
            conn.execute(
                "UPDATE plans SET name=%s,modules=%s,stripe_price_id=NULL WHERE slug=%s",
                (plan["name"], Jsonb(plan["modules"]), plan["slug"]),
            )
        users = {}
        for name, role in (("alice", "user"), ("bob", "user"), ("admin", "admin")):
            users[name] = conn.execute(
                "INSERT INTO users(id,tenant_id,email,name,password_hash,role,email_verified) VALUES (%s,%s,%s,%s,%s,%s,true) RETURNING *",
                (
                    uuid4(),
                    uuid4(),
                    name + "@example.com",
                    name.title(),
                    hasher.hash(PASSWORD),
                    role,
                ),
            ).fetchone()
    settings = Settings(
        _env_file=None,
        database_url=database["app"],
        session_secret="test-only-session-secret-long-enough-123",
        artifact_root=tmp_path / "artifacts",
        mail_root=tmp_path / "mail",
        max_artifact_bytes=1024,
        stripe_secret_key="sk_test_fixture",
        stripe_webhook_secret="whsec_fixture",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        yield {"client": client, "app": app, "settings": settings, "users": users, "db": database}


def auth(setup, name="alice"):
    response = setup["client"].post(
        "/api/v1/auth/login", json={"email": name + "@example.com", "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + response.json()["access_token"]}
