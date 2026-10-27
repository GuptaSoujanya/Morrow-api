import argparse
import getpass
import json
import os
import secrets
from pathlib import Path
from uuid import uuid4

import psycopg
from dotenv import load_dotenv
from fastapi import HTTPException
from psycopg.rows import dict_row
from pydantic import EmailStr, TypeAdapter

from .config import Settings
from .db import pool_for
from .mail import flush_mail
from .migrate import migrate
from .security import hasher


def init_local():
    path = Path(".env")
    if path.exists():
        raise ValueError(".env already exists; edit it directly to preserve your credentials")
    owner, app = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    text = f"""DATABASE_URL=postgresql://morrow_app:{app}@127.0.0.1:15433/morrow
DATABASE_OWNER_URL=postgresql://postgres:{owner}@127.0.0.1:15433/morrow
POSTGRES_PASSWORD={owner}
APP_DATABASE_PASSWORD={app}
SESSION_SECRET={secrets.token_urlsafe(48)}
PUBLIC_ORIGIN=http://127.0.0.1:8100
ARTIFACT_BACKEND=local
ARTIFACT_ROOT=.runtime/artifacts
MAIL_BACKEND=file
MAIL_ROOT=.runtime/mail
COOKIE_SECURE=false
"""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as file:
        file.write(text)
    print("Created private local configuration in .env. No secrets were printed.")


def owner_url():
    value = os.getenv("DATABASE_OWNER_URL")
    if not value:
        raise ValueError("DATABASE_OWNER_URL is required for this administrative command")
    return value


def provision(email, name, admin, password=None):
    email = str(TypeAdapter(EmailStr).validate_python(email)).lower()
    password = password or getpass.getpass("New password (at least 12 characters): ")
    if not 12 <= len(password) <= 128:
        raise ValueError("Use a password between 12 and 128 characters")
    with psycopg.connect(owner_url(), row_factory=dict_row) as conn:
        user = conn.execute(
            "INSERT INTO users(id,tenant_id,email,name,role,password_hash,email_verified) VALUES (%s,%s,%s,%s,%s,%s,true) RETURNING id,email",
            (uuid4(), uuid4(), email, name, "admin" if admin else "user", hasher.hash(password)),
        ).fetchone()
    return user


def link_firebase_account(email, uid, reason, settings, new_email=None):
    from .firebase_identity import FirebaseIdentity
    from .repository import audit

    if not 10 <= len(reason.strip()) <= 300 or any(ord(c) < 32 for c in reason):
        raise ValueError("Provide an audit reason between 10 and 300 characters")
    email = str(TypeAdapter(EmailStr).validate_python(email)).lower()
    target = str(TypeAdapter(EmailStr).validate_python(new_email)).lower() if new_email else email
    provider = FirebaseIdentity(settings)
    try:
        identity = provider.get_user(uid)
        if identity.disabled or not identity.email_verified:
            raise ValueError("Firebase identity must have a verified email and be enabled")
        if (identity.email or "").lower() != target:
            raise ValueError(
                "Firebase email must match the local account; use --new-email explicitly to migrate its email"
            )
        with psycopg.connect(owner_url(), row_factory=dict_row) as conn:
            user = conn.execute(
                "SELECT * FROM users WHERE email=%s FOR UPDATE", (email,)
            ).fetchone()
            if not user or not user["active"]:
                raise ValueError("Active local account not found")
            if user["firebase_uid"] and (
                user["firebase_uid"] != uid or user["firebase_project_id"] != provider.project_id
            ):
                raise ValueError("Account is already linked to another Firebase identity")
            conn.execute(
                "UPDATE users SET firebase_uid=%s,firebase_project_id=%s,email=%s,email_verified=true,password_hash=NULL,google_sub=NULL WHERE id=%s",
                (identity.uid, provider.project_id, target, user["id"]),
            )
            conn.execute("DELETE FROM auth_sessions WHERE user_id=%s", (user["id"],))
            conn.execute("DELETE FROM auth_tokens WHERE user_id=%s", (user["id"],))
            audit(
                conn,
                None,
                user["tenant_id"],
                "admin.firebase.link",
                str(user["id"]),
                "CLI database owner: " + reason.strip(),
            )
        return {
            "id": user["id"],
            "tenant_id": user["tenant_id"],
            "email": target,
            "role": user["role"],
        }
    finally:
        provider.close()


def main():
    parser = argparse.ArgumentParser(description="Morrow API administration")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-local")
    commands.add_parser("migrate")
    user = commands.add_parser("create-user")
    user.add_argument("--email", required=True)
    user.add_argument("--name", default="")
    user.add_argument("--admin", action="store_true")
    reset = commands.add_parser("set-password")
    reset.add_argument("--email", required=True)
    firebase = commands.add_parser(
        "link-firebase-user",
        help="Explicitly link an existing account; keeps its tenant, role, and plan",
    )
    firebase.add_argument("--email", required=True, help="Existing local account email")
    firebase.add_argument("--firebase-uid", required=True)
    firebase.add_argument(
        "--new-email", help="Explicitly replace a test/old email with the verified Firebase email"
    )
    firebase.add_argument("--reason", required=True)
    commands.add_parser("send-mail")
    commands.add_parser("reconcile-billing", help="Recover missed Razorpay billing notifications")
    importer = commands.add_parser("import-workspace")
    importer.add_argument("--source", type=Path, required=True)
    importer.add_argument("--owner-email", required=True)
    importer.add_argument(
        "--apply", action="store_true", help="Write the import; otherwise show counts only"
    )
    exporter = commands.add_parser(
        "export-workspace", help="Write an owner's account data into a Morrow user repo layout"
    )
    exporter.add_argument("--target", type=Path, required=True)
    exporter.add_argument("--owner-email", required=True)
    exporter.add_argument(
        "--apply", action="store_true", help="Write the files; otherwise show what would change"
    )
    commands.add_parser("purge-artifacts")
    args = parser.parse_args()
    load_dotenv()
    try:
        if args.command == "init-local":
            init_local()
        elif args.command == "migrate":
            migrate(owner_url(), os.environ["APP_DATABASE_PASSWORD"])
            print("Database migrations and runtime grants applied.")
        elif args.command == "create-user":
            user = provision(args.email, args.name, args.admin)
            print(f"Created {'administrator' if args.admin else 'user'} {user['email']}")
        elif args.command == "link-firebase-user":
            linked = link_firebase_account(
                args.email, args.firebase_uid, args.reason, Settings(), args.new_email
            )
            print(
                f"Linked Firebase identity to {linked['email']}; tenant, role, plan, and data preserved. Existing sessions revoked."
            )
        elif args.command == "set-password":
            password = getpass.getpass("New password (at least 12 characters): ")
            if not 12 <= len(password) <= 128:
                raise ValueError("Use a password between 12 and 128 characters")
            with psycopg.connect(owner_url()) as conn:
                row = conn.execute(
                    "UPDATE users SET password_hash=%s WHERE email=%s AND firebase_uid IS NULL RETURNING id",
                    (hasher.hash(password), args.email.lower()),
                ).fetchone()
                if not row:
                    raise ValueError(
                        "Local-password user not found; Firebase users reset passwords through Firebase"
                    )
                conn.execute("DELETE FROM auth_sessions WHERE user_id=%s", (row[0],))
            print("Password updated and existing sessions revoked.")
        elif args.command == "import-workspace":
            from .importer import import_workspace

            print(
                json.dumps(
                    import_workspace(
                        args.source, args.owner_email, args.apply, Settings(), owner_url()
                    ),
                    indent=2,
                )
            )
        elif args.command == "export-workspace":
            from .exporter import export_workspace

            print(
                json.dumps(
                    export_workspace(
                        args.owner_email, args.target, args.apply, Settings(), owner_url()
                    ),
                    indent=2,
                )
            )
        else:
            settings = Settings()
            with pool_for(settings.database_url.get_secret_value()) as pool:
                if args.command == "send-mail":
                    print(f"Delivered {flush_mail(pool, settings)} queued messages.")
                elif args.command == "reconcile-billing":
                    from .reconciliation import reconcile_billing

                    result = reconcile_billing(settings, pool)
                    print(json.dumps(result))
                    if result["failed"]:
                        parser.exit(1, "Some subscriptions need another reconciliation attempt.\n")
                elif args.command == "purge-artifacts":
                    from .storage import storage_for

                    storage = storage_for(settings)
                    with psycopg.connect(owner_url(), row_factory=dict_row) as conn:
                        rows = conn.execute(
                            "SELECT object_key FROM artifacts WHERE deleted_at IS NOT NULL"
                        ).fetchall()
                    for row in rows:
                        storage.delete(row["object_key"])
                    print(f"Purged {len(rows)} tombstoned objects.")
    except (ValueError, KeyError, psycopg.Error, HTTPException) as exc:
        # Connection exceptions can contain database credentials; never print them.
        message = (
            str(exc)
            if isinstance(exc, ValueError)
            else str(exc.detail)
            if isinstance(exc, HTTPException)
            else "Operation failed; check database configuration and input."
        )
        parser.exit(1, message + "\n")


if __name__ == "__main__":
    main()
