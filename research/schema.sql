-- =============================================================================
-- market_context: structured LLM market-regime signal consumed by the strategy
-- soft gate. Loaded by the postgres container on first init (see docker-compose).
-- TimescaleDB is optional; the hypertable conversion is wrapped so this file
-- also works on vanilla Postgres.
-- =============================================================================

CREATE TABLE IF NOT EXISTS market_context (
    id             BIGSERIAL PRIMARY KEY,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    regime         TEXT NOT NULL CHECK (regime IN ('trending_up','trending_down','ranging','high_vol')),
    risk_state     TEXT NOT NULL CHECK (risk_state IN ('risk_on','risk_off','neutral')),
    confidence     DOUBLE PRECISION NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    rationale      TEXT NOT NULL DEFAULT '',
    notable_events JSONB NOT NULL DEFAULT '[]'::jsonb,
    source_model   TEXT NOT NULL DEFAULT '',
    headlines_hash TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_market_context_created_at
    ON market_context (created_at DESC);

-- Optional: make it a TimescaleDB hypertable when the extension is present.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb') THEN
        CREATE EXTENSION IF NOT EXISTS timescaledb;
        PERFORM create_hypertable('market_context', 'created_at',
                                  if_not_exists => TRUE, migrate_data => TRUE);
    END IF;
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'Skipping hypertable conversion: %', SQLERRM;
END $$;
