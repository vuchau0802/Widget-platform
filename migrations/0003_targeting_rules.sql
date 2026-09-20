ALTER TABLE widgets ADD COLUMN IF NOT EXISTS targeting_rules JSONB NOT NULL DEFAULT '{}';

COMMENT ON COLUMN widgets.targeting_rules IS 'Config-driven targeting: page_paths (text[]), delay_seconds (int), once_per_visitor (bool)';
