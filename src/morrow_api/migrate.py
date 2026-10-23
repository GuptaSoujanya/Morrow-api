import hashlib
from importlib.resources import files

import psycopg
from psycopg import sql


def migrate(owner_url: str, app_password: str) -> None:
    if len(app_password) < 24:
        raise ValueError("Use an application database password of at least 24 characters")
    with psycopg.connect(owner_url) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(775011)")
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version text PRIMARY KEY, sha256 text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())")
        for path in sorted(files("morrow_api").joinpath("migrations").iterdir(), key=lambda x: x.name):
            if not path.name.endswith(".sql"):
                continue
            source = path.read_text()
            version = path.name.removesuffix(".sql")
            checksum = hashlib.sha256(source.encode()).hexdigest()
            existing = conn.execute("SELECT sha256 FROM schema_migrations WHERE version=%s", (version,)).fetchone()
            if existing:
                if existing[0] != checksum:
                    raise RuntimeError(f"Applied migration changed: {version}")
                continue
            conn.execute(source)
            conn.execute("INSERT INTO schema_migrations(version, sha256) VALUES (%s,%s)", (version, checksum))
        if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname='morrow_app'").fetchone():
            conn.execute("CREATE ROLE morrow_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS")
        conn.execute(sql.SQL("ALTER ROLE morrow_app PASSWORD {}" ).format(sql.Literal(app_password)))
        conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        conn.execute("GRANT USAGE ON SCHEMA public TO morrow_app")
        conn.execute("GRANT SELECT ON schema_migrations, users TO morrow_app")
        conn.execute("GRANT SELECT, INSERT, DELETE ON auth_sessions TO morrow_app")
        conn.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON auth_limits TO morrow_app")
        conn.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON profiles, applications, workspace_records, artifacts TO morrow_app")
        conn.execute("GRANT SELECT, INSERT ON profile_versions TO morrow_app")
        conn.execute("GRANT SELECT, INSERT ON audit_events TO morrow_app")
        conn.execute("GRANT USAGE ON SEQUENCE audit_events_id_seq TO morrow_app")
        conn.execute("GRANT INSERT, UPDATE ON users TO morrow_app")
        conn.execute("GRANT SELECT, UPDATE ON plans TO morrow_app")
        conn.execute("GRANT SELECT, INSERT, DELETE ON auth_tokens, oauth_states TO morrow_app")
        conn.execute("GRANT SELECT, INSERT, UPDATE ON mail_outbox TO morrow_app")
        conn.execute("GRANT SELECT, INSERT ON stripe_events TO morrow_app")
