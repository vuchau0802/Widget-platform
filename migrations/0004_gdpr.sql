ALTER TABLE submissions ADD COLUMN IF NOT EXISTS confirmed_at TIMESTAMPTZ;
ALTER TABLE submissions ADD COLUMN IF NOT EXISTS confirmation_token TEXT;
ALTER TABLE submissions ADD COLUMN IF NOT EXISTS consent_given BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE submissions ADD COLUMN IF NOT EXISTS consent_timestamp TIMESTAMPTZ;
ALTER TABLE submissions ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;

CREATE UNIQUE INDEX IF NOT EXISTS idx_submissions_confirmation_token
    ON submissions(confirmation_token) WHERE confirmation_token IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_submissions_tenant_widget
    ON submissions(tenant_id, widget_id);
