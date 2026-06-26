-- 138_data_usage_map_shape_tolerant.sql
--
-- Fix: data_usage_map (created in 026_data_ledger.sql) assumed
-- strategy_registry.parameters->'required_columns' is ALWAYS a flat JSON array of
-- column-name strings, e.g. ["prices","returns"]. The richer requirements schema
-- adopted later stores it as an OBJECT instead:
--     {"required":[...], "optional":[...]}
-- whose inner elements are either bare strings ("prices") OR objects
-- ({"column":"prices","provider":...,"refresh":...}). Calling
-- jsonb_array_elements_text() on that object throws
--     ERROR: cannot extract elements from an object
-- The COALESCE(...,'[]') guard only covered the NULL/absent case, not the object
-- case. As of 2026-06-26, 31 of 188 registry rows are object-form, 13 flat-array,
-- 144 absent — so the view (and therefore `REFRESH MATERIALIZED VIEW data_ledger`,
-- run at every johnbot boot by sync_data_ledger.py) errored every time. The failure
-- was silent: bot.js catches it ("data_ledger sync skipped") and boots anyway, so
-- data_columns coverage stats silently stopped updating.
--
-- This rebuilds data_usage_map to extract column names from EVERY observed shape:
--   * flat array            -> its elements
--   * object.required array -> its elements   (the must-have data deps)
--   * object.optional array -> its elements   (also a consumer for usage/cost purposes)
--   * each element: a bare string, or {"column":"<name>",...}
-- Unrecognised / malformed shapes contribute nothing instead of throwing (defensive).
-- Output columns (column_name, users) are unchanged, so CREATE OR REPLACE keeps the
-- dependent data_ledger materialized view valid. Idempotent — safe to re-run at boot.

CREATE OR REPLACE VIEW data_usage_map AS
WITH rc AS (
    SELECT sr.id, sr.status, sr.parameters -> 'required_columns' AS v
    FROM   strategy_registry sr
    WHERE  sr.parameters ? 'required_columns'
),
elems AS (
    -- flat array form: ["prices", ...] or [{"column":"prices",...}, ...]
    SELECT id, status, jsonb_array_elements(v) AS e
    FROM   rc
    WHERE  jsonb_typeof(v) = 'array'
    UNION ALL
    -- object form: {"required":[...], "optional":[...]} — take both lists
    SELECT id, status, jsonb_array_elements(v -> 'required')
    FROM   rc
    WHERE  jsonb_typeof(v) = 'object' AND jsonb_typeof(v -> 'required') = 'array'
    UNION ALL
    SELECT id, status, jsonb_array_elements(v -> 'optional')
    FROM   rc
    WHERE  jsonb_typeof(v) = 'object' AND jsonb_typeof(v -> 'optional') = 'array'
),
names AS (
    SELECT id, status,
           CASE jsonb_typeof(e)
               WHEN 'string' THEN e #>> '{}'      -- bare json string -> text
               WHEN 'object' THEN e ->> 'column'  -- {"column":"name",...} -> name
           END AS column_name
    FROM   elems
)
SELECT column_name,
       array_agg(DISTINCT id) FILTER (WHERE status IN ('approved', 'active')) AS users
FROM   names
WHERE  column_name IS NOT NULL
GROUP  BY column_name;
