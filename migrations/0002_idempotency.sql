ALTER TABLE submissions ADD COLUMN IF NOT EXISTS idempotency_key TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_submissions_tenant_idempotency ON submissions(tenant_id, idempotency_key);