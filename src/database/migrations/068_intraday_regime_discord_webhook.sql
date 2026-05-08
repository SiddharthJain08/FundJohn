-- 068_intraday_regime_discord_webhook.sql
-- DOCUMENTATION ONLY — operator action required, no automatic insert.
--
-- The intraday HMM detector calls _post_to_discord('intraday-regime', ...)
-- on every confirmed transition + dry-run heartbeat. The webhook lookup
-- queries agent_registry.webhook_urls (JSONB) for a key 'intraday-regime'.
-- Until that key is populated, posts no-op silently (current behaviour:
-- "no webhook for #intraday-regime" log line).
--
-- To enable Discord notifications:
--   1. Create channel #intraday-regime in the Discord guild.
--   2. Edit channel → Integrations → Webhooks → New Webhook → copy URL.
--   3. Run the UPDATE below (replace WEBHOOK_URL with the actual URL).
--
-- Example (replace placeholder URL before running):
--
-- UPDATE agent_registry
--    SET webhook_urls = COALESCE(webhook_urls, '{}'::jsonb) ||
--        jsonb_build_object('intraday-regime',
--                            'https://discord.com/api/webhooks/PLACEHOLDER/PLACEHOLDER')
--  WHERE id = 'botjohn';
--
-- Routing rationale: tradedesk handles trade-reports + position-recs;
-- botjohn handles general + botjohn-log. The intraday-regime channel
-- is monitoring/observability, closer to botjohn-log than to a trade
-- desk feed, so it lives on the botjohn agent. Adjust if your team
-- prefers tradedesk.

SELECT 'intraday-regime webhook registration is documentation only — see comments above'
       AS reminder;
