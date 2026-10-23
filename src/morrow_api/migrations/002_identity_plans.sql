CREATE TABLE plans (
    slug text PRIMARY KEY,
    name text NOT NULL,
    description text NOT NULL,
    monthly_amount integer NOT NULL CHECK (monthly_amount >= 0),
    currency text NOT NULL DEFAULT 'inr',
    modules jsonb NOT NULL CHECK (jsonb_typeof(modules)='array'),
    stripe_price_id text UNIQUE,
    active boolean NOT NULL DEFAULT true
);
INSERT INTO plans(slug,name,description,monthly_amount,modules) VALUES
('free','Free','Keep your career essentials in one place.',0,'["profile","applications","files"]'),
('plus','Plus','Prepare stronger applications with your own AI.',49900,'["profile","applications","files","resumes","research","learning"]'),
('pro','Pro','Your full career workspace, connected.',99900,'["profile","applications","files","resumes","research","learning","automations","outreach","analytics"]');

ALTER TABLE users ADD COLUMN name text NOT NULL DEFAULT '';
ALTER TABLE users ADD COLUMN email_verified boolean NOT NULL DEFAULT false;
ALTER TABLE users ADD COLUMN google_sub text UNIQUE;
ALTER TABLE users ADD COLUMN plan_slug text NOT NULL DEFAULT 'free' REFERENCES plans(slug);
ALTER TABLE users ADD COLUMN plan_source text NOT NULL DEFAULT 'free' CHECK (plan_source IN ('free','admin','stripe'));
ALTER TABLE users ADD COLUMN plan_expires_at timestamptz;
ALTER TABLE users ADD COLUMN stripe_customer_id text UNIQUE;
ALTER TABLE users ADD COLUMN stripe_subscription_id text UNIQUE;
ALTER TABLE users ADD COLUMN subscription_status text NOT NULL DEFAULT 'none';
ALTER TABLE users ADD COLUMN ai_provider text NOT NULL DEFAULT 'claude' CHECK (ai_provider IN ('claude','kiro'));
ALTER TABLE auth_sessions ADD COLUMN csrf_hash text NOT NULL DEFAULT '';

CREATE TABLE auth_tokens (
    token_hash text PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    purpose text NOT NULL CHECK (purpose IN ('verify','reset')),
    expires_at timestamptz NOT NULL
);
CREATE TABLE oauth_states (
    state_hash text PRIMARY KEY,
    expires_at timestamptz NOT NULL
);
CREATE TABLE mail_outbox (
    id uuid PRIMARY KEY,
    recipient text NOT NULL,
    subject text NOT NULL,
    body text NOT NULL,
    attempts integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    sent_at timestamptz
);
CREATE TABLE stripe_events (
    id text PRIMARY KEY,
    event_type text NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT now()
);
