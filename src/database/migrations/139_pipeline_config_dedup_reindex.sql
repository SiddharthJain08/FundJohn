-- 139_pipeline_config_dedup_reindex.sql
-- Repair a corrupt primary key. pipeline_config has PRIMARY KEY (key), but byte-identical
-- duplicate key rows exist (collection_enabled, collect_technicals) — the unique index
-- stopped enforcing, almost certainly from an OOM/crash mid-write. Delete the redundant
-- row of each key (values are identical, so which physical copy survives is immaterial),
-- then REINDEX to restore the uniqueness guarantee the ON CONFLICT (key) writers
-- (src/channels/api/server.js, src/pipeline/store.js) depend on.
-- Idempotent: on a clean table the DELETE affects 0 rows and the REINDEX safely rebuilds.
DELETE FROM pipeline_config a
  USING pipeline_config b
  WHERE a.ctid > b.ctid
    AND a.key = b.key;

REINDEX TABLE pipeline_config;
