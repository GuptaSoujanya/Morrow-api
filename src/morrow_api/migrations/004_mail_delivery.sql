ALTER TABLE mail_outbox ADD COLUMN next_attempt_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE mail_outbox ADD COLUMN expires_at timestamptz;
ALTER TABLE mail_outbox ADD COLUMN last_error_code text;

CREATE INDEX mail_outbox_pending_delivery
    ON mail_outbox(next_attempt_at, created_at)
    WHERE sent_at IS NULL AND attempts < 5;
