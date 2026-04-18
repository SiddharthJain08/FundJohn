-- data_usage_map: unrolls required_columns from strategy_registry parameters JSONB
CREATE VIEW data_usage_map AS
SELECT r.column_name,
       array_agg(DISTINCT sr.id) FILTER (WHERE sr.status IN ('approved', 'active')) AS users
FROM   strategy_registry sr,
       LATERAL jsonb_array_elements_text(
         COALESCE(sr.parameters -> 'required_columns', '[]'::jsonb)
       ) AS r(column_name)
GROUP BY r.column_name;

-- data_ledger: shared surface queried by all research agents
CREATE MATERIALIZED VIEW data_ledger AS
SELECT  c.column_name,
        c.provider,
        c.introduced_at,
        c.refresh_cadence,
        c.estimated_monthly_cost,
        COALESCE(u.users, ARRAY[]::text[]) AS current_users,
        MAX(r.read_at)                     AS last_consumed_at
FROM    data_columns c
LEFT JOIN data_usage_map u  USING (column_name)
LEFT JOIN signal_cache_reads r USING (column_name)
GROUP BY c.column_name, c.provider, c.introduced_at,
         c.refresh_cadence, c.estimated_monthly_cost, u.users;

CREATE UNIQUE INDEX data_ledger_column_name_idx ON data_ledger (column_name);
