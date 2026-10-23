CREATE TABLE users (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL UNIQUE,
    email text NOT NULL UNIQUE CHECK (email = lower(email)),
    password_hash text,
    role text NOT NULL DEFAULT 'user' CHECK (role IN ('user', 'admin')),
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE auth_sessions (
    token_hash text PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX auth_sessions_expiry ON auth_sessions(expires_at);
CREATE TABLE auth_limits (
    key text PRIMARY KEY,
    attempts integer NOT NULL,
    window_start timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE profiles (
    tenant_id uuid PRIMARY KEY REFERENCES users(tenant_id),
    version integer NOT NULL CHECK (version > 0),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload)='object'),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE profile_versions (
    tenant_id uuid NOT NULL REFERENCES users(tenant_id),
    version integer NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, version)
);

CREATE TABLE applications (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES users(tenant_id),
    idempotency_key text NOT NULL,
    request_hash text NOT NULL,
    platform text NOT NULL,
    external_job_id text NOT NULL,
    title text NOT NULL DEFAULT '',
    company text NOT NULL DEFAULT '',
    url text NOT NULL DEFAULT '',
    status text NOT NULL,
    detail text NOT NULL DEFAULT '',
    occurred_at timestamptz NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, idempotency_key)
);
CREATE INDEX applications_tenant_time ON applications(tenant_id, occurred_at DESC, id DESC);

CREATE TABLE workspace_records (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES users(tenant_id),
    collection text NOT NULL,
    record_key text NOT NULL,
    payload jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, collection, record_key)
);

CREATE TABLE artifacts (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES users(tenant_id),
    filename text NOT NULL,
    media_type text NOT NULL,
    category text NOT NULL,
    object_key text NOT NULL UNIQUE,
    sha256 text NOT NULL,
    size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
    source_key text,
    created_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    UNIQUE (tenant_id, source_key)
);
CREATE INDEX artifacts_tenant_time ON artifacts(tenant_id, created_at DESC, id DESC);

CREATE TABLE audit_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    actor_id uuid REFERENCES users(id),
    target_tenant_id uuid REFERENCES users(tenant_id),
    action text NOT NULL,
    resource text NOT NULL,
    reason text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

DO $$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY['profiles', 'profile_versions', 'applications', 'workspace_records', 'artifacts'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format(
            'CREATE POLICY tenant_scope ON %I USING (tenant_id = nullif(current_setting(''morrow.tenant_id'', true), '''')::uuid) WITH CHECK (tenant_id = nullif(current_setting(''morrow.tenant_id'', true), '''')::uuid)',
            table_name
        );
    END LOOP;
END $$;
