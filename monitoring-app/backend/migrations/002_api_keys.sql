-- Migration 002: API keys + usage logs
--
-- Note: the backend also auto-creates these tables on startup via
-- SQLAlchemy `Base.metadata.create_all(...)`, so this file is optional.
-- Run it manually only if you prefer an explicit migration step.
-- UUIDs are stored as CHAR(36) (application-generated UUIDv4) to stay
-- portable across plain PostgreSQL and TimescaleDB without extensions.

CREATE TABLE IF NOT EXISTS api_keys (
    id                CHAR(36) PRIMARY KEY,
    name              VARCHAR(255) NOT NULL,
    key_hash          VARCHAR(255) NOT NULL UNIQUE,   -- SHA256 hex digest
    prefix            VARCHAR(20),                    -- first 6 chars, for display
    created_at        TIMESTAMPTZ,
    last_used_at      TIMESTAMPTZ,
    is_active         BOOLEAN NOT NULL DEFAULT TRUE,
    rate_limit        INTEGER DEFAULT 60,             -- requests / minute
    daily_token_limit INTEGER DEFAULT 1000000,        -- tokens / day
    total_requests    INTEGER DEFAULT 0,
    total_tokens      INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS ix_api_keys_key_hash ON api_keys (key_hash);

CREATE TABLE IF NOT EXISTS api_usage_logs (
    id              CHAR(36) PRIMARY KEY,
    api_key_id      CHAR(36) REFERENCES api_keys (id) ON DELETE CASCADE,
    request_time    TIMESTAMPTZ,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    total_tokens    INTEGER,
    endpoint        VARCHAR(255),
    status_code     INTEGER,
    ip_address      VARCHAR(45)
);

CREATE INDEX IF NOT EXISTS ix_api_usage_logs_api_key_id ON api_usage_logs (api_key_id);
CREATE INDEX IF NOT EXISTS ix_api_usage_logs_request_time ON api_usage_logs (request_time);