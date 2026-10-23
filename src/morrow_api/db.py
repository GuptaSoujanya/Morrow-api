from contextlib import contextmanager
from uuid import UUID

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


def pool_for(url: str) -> ConnectionPool:
    return ConnectionPool(
        url,
        min_size=1,
        max_size=8,
        timeout=10,
        open=False,
        kwargs={"row_factory": dict_row, "autocommit": True},
    )


def assert_runtime_role(pool: ConnectionPool) -> None:
    with pool.connection() as conn:
        role = conn.execute(
            "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb FROM pg_roles WHERE rolname=current_user"
        ).fetchone()
        owned = conn.execute(
            "SELECT count(*) AS n FROM pg_tables WHERE schemaname='public' AND tableowner=current_user"
        ).fetchone()["n"]
        if any(role.values()) or owned:
            raise RuntimeError(
                "API must use a non-owner database role without privileged role flags"
            )
        version = conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if not version or version["version"] != "004_mail_delivery":
            raise RuntimeError("Run database migrations before starting the API")


@contextmanager
def tenant_transaction(pool: ConnectionPool, tenant_id: UUID):
    with pool.connection() as conn, conn.transaction():
        conn.execute("SELECT set_config('morrow.tenant_id', %s, true)", (str(tenant_id),))
        yield conn


def set_tenant(conn, tenant_id: UUID) -> None:
    conn.execute("SELECT set_config('morrow.tenant_id', %s, true)", (str(tenant_id),))
