-- 003: per-request latency for API-key gateway accounting.
--
-- Adds the end-to-end response duration of each proxied request
-- (api_usage_logs.latency_ms) and a lifetime latency accumulator on
-- api_keys, which the UI turns into "средняя скорость ответа" (ток/с)
-- and "средняя задержка" (мс) per key.
--
-- Note: the backend applies the same idempotent ALTERs automatically on
-- startup (see database.py init_db), so this file is optional — run it
-- manually only if you prefer an explicit migration step.

ALTER TABLE api_usage_logs ADD COLUMN IF NOT EXISTS latency_ms INTEGER;

ALTER TABLE api_keys
    ADD COLUMN IF NOT EXISTS total_latency_ms BIGINT NOT NULL DEFAULT 0;
