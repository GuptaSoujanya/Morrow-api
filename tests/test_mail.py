from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from threading import Barrier
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from morrow_api import mail
from morrow_api.config import Settings


def settings_for(tmp_path, **overrides):
    return Settings(
        _env_file=None,
        database_url="postgresql://test:test@localhost/test",
        mail_root=tmp_path / "mail",
        mail_from="Morrow <hello@updates.example.com>",
        public_origin="https://app.example.com",
        **overrides,
    )


def row_for(settings):
    return {
        "id": uuid4(),
        "recipient": "recipient@example.com",
        "subject": "Verify your email · Morrow",
        "body": f"Verify your email\n\n{settings.public_origin}/#/verify?token=synthetic_token\n\nThis link expires in 24 hours. Ignore it if you did not request this.",
    }


def enqueue(setup, *, subject="Synthetic message", body="Private test body", expires_at=None):
    with setup["app"].state.pool.connection() as conn, conn.transaction():
        mail.queue_mail(conn, "recipient@example.com", subject, body, expires_at=expires_at)


def rows(setup):
    with setup["app"].state.pool.connection() as conn:
        return conn.execute("SELECT * FROM mail_outbox ORDER BY created_at,id").fetchall()


def use_resend(setup):
    setup["settings"].mail_backend = "resend"
    setup["settings"].resend_api_key = SecretStr("re_synthetic_secret")


def test_file_transport_has_private_permissions_branded_html_and_plain_fallback(tmp_path):
    settings = settings_for(tmp_path)
    row = row_for(settings)
    mail.deliver(row, settings)
    saved = settings.mail_root / f"{row['id']}.eml"
    assert saved.stat().st_mode & 0o777 == 0o600
    assert settings.mail_root.stat().st_mode & 0o777 == 0o700
    message = BytesParser(policy=policy.default).parsebytes(saved.read_bytes())
    assert message.get_body(preferencelist=("plain",)).get_content().strip() == row["body"]
    rendered = message.get_body(preferencelist=("html",)).get_content()
    assert "morrow" in rendered and "background:#b9a5ff" in rendered
    assert f'href="{settings.public_origin}/#/verify?token=synthetic_token"' in rendered
    assert "<img" not in rendered and "<script" not in rendered


def test_html_escapes_message_content_and_only_links_the_exact_application_origin(tmp_path):
    settings = settings_for(tmp_path)
    rendered = mail.html_body(
        "<script>Subject</script>",
        "<img src=x onerror=alert(1)>\n\nPrivate & safe",
        settings.public_origin,
    )
    assert "<script>" not in rendered and "<img " not in rendered
    assert "&lt;script&gt;" in rendered and "Private &amp; safe" in rendered
    rendered = mail.html_body(
        "Reset your password · Morrow",
        "Reset your password\n\nhttps://app.example.com.evil.test/#/reset?token=synthetic",
        settings.public_origin,
    )
    assert "href=" not in rendered
    assert "src=" not in rendered


def test_smtp_preserves_starttls_auth_and_both_message_parts(tmp_path, monkeypatch):
    settings = settings_for(
        tmp_path,
        mail_backend="smtp",
        smtp_host="smtp.example.com",
        smtp_username="mailer",
        smtp_password="synthetic",
    )
    calls = []

    class SMTP:
        def __init__(self, host, port, timeout):
            calls.append(("connect", host, port, timeout))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def starttls(self, context):
            assert context.check_hostname
            calls.append(("tls",))

        def login(self, username, password):
            calls.append(("login", username, password))

        def send_message(self, message):
            assert message.get_body(preferencelist=("plain",))
            assert message.get_body(preferencelist=("html",))
            calls.append(("send", message["To"]))

    monkeypatch.setattr(mail.smtplib, "SMTP", SMTP)
    mail.deliver(row_for(settings), settings)
    assert [call[0] for call in calls] == ["connect", "tls", "login", "send"]


def test_resend_uses_fixed_tls_endpoint_stable_idempotency_and_text_html(tmp_path, monkeypatch):
    settings = settings_for(tmp_path, mail_backend="resend", resend_api_key="re_synthetic_secret")
    row, requests = row_for(settings), []

    def post(url, **kwargs):
        requests.append((url, kwargs))
        return httpx.Response(200, json={"id": "synthetic-provider-id"})

    monkeypatch.setattr(mail.httpx, "post", post)
    mail.deliver(row, settings)
    mail.deliver(row, settings)
    assert requests[0] == requests[1]
    url, options = requests[0]
    assert url == "https://api.resend.com/emails"
    assert options["follow_redirects"] is False and options["timeout"] == 15
    assert options["headers"]["Idempotency-Key"] == "morrow-mail/" + str(row["id"])
    assert options["headers"]["Authorization"] == "Bearer re_synthetic_secret"
    assert options["json"]["text"] == row["body"] and "<html" in options["json"]["html"]
    assert options["json"]["to"] == [row["recipient"]]
    assert "re_synthetic_secret" not in repr(settings)


@pytest.mark.parametrize(
    "response,code",
    [
        (httpx.Response(302, headers={"Location": "https://attacker.test"}), "resend_http_302"),
        (httpx.Response(200, json={}), "resend_unconfirmed_response"),
        (
            httpx.Response(403, json={"name": "private_token_value", "message": "private body"}),
            "resend_http_403",
        ),
    ],
)
def test_resend_requires_confirmed_success_and_sanitizes_provider_errors(
    tmp_path, monkeypatch, response, code
):
    settings = settings_for(tmp_path, mail_backend="resend", resend_api_key="re_synthetic")
    monkeypatch.setattr(mail.httpx, "post", lambda *args, **kwargs: response)
    with pytest.raises(mail.DeliveryError) as error:
        mail.deliver(row_for(settings), settings)
    assert error.value.code == code
    assert "secret" not in str(error.value) and "private body" not in str(error.value)


def test_success_marks_queue_sent_and_removes_recovery_link(setup):
    enqueue(setup, body="Private test recovery link")
    assert mail.flush_mail(setup["app"].state.pool, setup["settings"]) == 1
    row = rows(setup)[0]
    assert row["sent_at"] and row["attempts"] == 1
    assert row["body"] == "" and row["last_error_code"] is None


def test_transport_failure_is_backed_off_and_retries_with_the_same_idempotency_key(
    setup, monkeypatch
):
    use_resend(setup)
    enqueue(setup)
    attempts = []

    def post(url, **kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            raise httpx.ConnectTimeout("synthetic failure without any real network request")
        return httpx.Response(200, json={"id": "synthetic-id"})

    monkeypatch.setattr(mail.httpx, "post", post)
    assert mail.flush_mail(setup["app"].state.pool, setup["settings"]) == 0
    row = rows(setup)[0]
    assert row["attempts"] == 1 and row["sent_at"] is None and row["body"]
    assert row["last_error_code"] == "transport_unavailable"
    assert row["next_attempt_at"] > datetime.now(timezone.utc)
    assert mail.flush_mail(setup["app"].state.pool, setup["settings"]) == 0
    assert len(attempts) == 1
    with setup["app"].state.pool.connection() as conn:
        conn.execute("UPDATE mail_outbox SET next_attempt_at=now()-interval '1 second'")
    assert mail.flush_mail(setup["app"].state.pool, setup["settings"]) == 1
    assert attempts[0] == attempts[1]
    assert rows(setup)[0]["body"] == ""


def test_daily_quota_pauses_current_queue_until_next_utc_day(setup, monkeypatch):
    use_resend(setup)
    enqueue(setup, subject="One")
    enqueue(setup, subject="Two")
    calls = []

    def post(*args, **kwargs):
        calls.append(kwargs)
        return httpx.Response(429, json={"name": "daily_quota_exceeded"})

    monkeypatch.setattr(mail.httpx, "post", post)
    assert mail.flush_mail(setup["app"].state.pool, setup["settings"]) == 0
    queued = rows(setup)
    assert len(calls) == 1 and sum(row["attempts"] for row in queued) == 1
    # The driver returns timestamptz in the session's zone; compare in UTC.
    retries = [row["next_attempt_at"].astimezone(timezone.utc) for row in queued]
    assert all(
        retry.date() == (datetime.now(timezone.utc) + timedelta(days=1)).date() for retry in retries
    )
    assert all(retry.hour == 0 and retry.second == 5 for retry in retries)


def test_expired_auth_link_is_discarded_without_delivery(setup, monkeypatch):
    enqueue(
        setup,
        body="expired-private-token",
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    monkeypatch.setattr(
        mail, "deliver", lambda *args: pytest.fail("Expired mail must not be delivered")
    )
    assert mail.flush_mail(setup["app"].state.pool, setup["settings"]) == 0
    row = rows(setup)[0]
    assert row["body"] == "" and row["last_error_code"] == "expired"
    assert row["sent_at"] is None and row["attempts"] == 0


def test_concurrent_quota_failures_do_not_deadlock_outbox_locks(setup, monkeypatch):
    use_resend(setup)
    enqueue(setup, subject="One")
    enqueue(setup, subject="Two")
    barrier = Barrier(2)

    def post(*args, **kwargs):
        barrier.wait(timeout=3)
        return httpx.Response(
            429, json={"name": "rate_limit_exceeded"}, headers={"Retry-After": "120"}
        )

    monkeypatch.setattr(mail.httpx, "post", post)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(mail.flush_mail, setup["app"].state.pool, setup["settings"], 1)
            for _ in range(2)
        ]
        assert [future.result(timeout=5) for future in futures] == [0, 0]
    assert [row["attempts"] for row in rows(setup)] == [1, 1]
    assert all(row["last_error_code"] == "rate_limit_exceeded" for row in rows(setup))
