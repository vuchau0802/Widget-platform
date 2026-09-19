CREATE TABLE IF NOT EXISTS tenants (
    id SERIAL PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    supabase_user_id TEXT UNIQUE,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS widgets (
    id SERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    fields JSONB NOT NULL,
    button_text TEXT DEFAULT 'Submit',
    bundle_version INTEGER DEFAULT 1,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_widgets_tenant ON widgets(tenant_id);

CREATE TABLE IF NOT EXISTS submissions (
    id SERIAL PRIMARY KEY,
    widget_id INTEGER NOT NULL REFERENCES widgets(id),
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    data JSONB NOT NULL,
    ip_address TEXT,
    country TEXT,
    city TEXT,
    spam_flag BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_submissions_widget ON submissions(widget_id);
CREATE INDEX IF NOT EXISTS idx_submissions_tenant ON submissions(tenant_id);
CREATE INDEX IF NOT EXISTS idx_submissions_created ON submissions(created_at);