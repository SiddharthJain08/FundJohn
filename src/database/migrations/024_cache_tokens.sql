ALTER TABLE subagent_costs
    ADD COLUMN IF NOT EXISTS cache_write_tokens INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS cache_read_tokens  INTEGER DEFAULT 0;
