-- Add configurable daily trigger time to pipeline_config

INSERT INTO pipeline_config (key, value, description) VALUES
    ('daily_trigger_time', '16:30',            'Daily collection trigger time HH:MM (24h) in daily_trigger_tz'),
    ('daily_trigger_tz',   'America/New_York',  'Timezone for daily_trigger_time — handles DST automatically')
ON CONFLICT (key) DO NOTHING;
