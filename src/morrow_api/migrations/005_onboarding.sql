CREATE TABLE onboarding_state (
    tenant_id uuid PRIMARY KEY REFERENCES users(tenant_id) ON DELETE CASCADE,
    version integer NOT NULL CHECK (version > 0),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    completed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE onboarding_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE onboarding_state FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON onboarding_state
    USING (tenant_id = nullif(current_setting('morrow.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('morrow.tenant_id', true), '')::uuid);
