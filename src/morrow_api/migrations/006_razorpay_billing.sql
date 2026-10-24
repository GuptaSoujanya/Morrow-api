ALTER TABLE plans ADD COLUMN razorpay_plan_id text UNIQUE;
ALTER TABLE users DROP CONSTRAINT users_plan_source_check;
ALTER TABLE users ADD CONSTRAINT users_plan_source_check
    CHECK (plan_source IN ('free','admin','stripe','razorpay'));

-- Commit this attempt before calling a provider that does not document create
-- idempotency. An unknown result remains reserved until it can be reconciled.
CREATE TABLE razorpay_subscriptions (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider_subscription_id text UNIQUE,
    provider_plan_id text NOT NULL,
    plan_slug text NOT NULL REFERENCES plans(slug),
    plan_name text NOT NULL,
    monthly_amount integer NOT NULL CHECK (monthly_amount > 0),
    currency text NOT NULL CHECK (currency = 'INR'),
    status text NOT NULL CHECK (status IN (
        'creating','creation_unknown','creation_failed','created','authenticated',
        'active','pending','halted','paused','cancelled','completed','expired'
    )),
    paid_through timestamptz,
    cancel_at_period_end boolean NOT NULL DEFAULT false,
    cancel_requested boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX razorpay_one_open_subscription ON razorpay_subscriptions(user_id)
    WHERE status NOT IN ('creation_failed','cancelled','completed','expired');
CREATE INDEX razorpay_subscription_owner ON razorpay_subscriptions(user_id,created_at DESC);

CREATE TABLE razorpay_events (
    id text PRIMARY KEY,
    event_type text NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT now()
);
