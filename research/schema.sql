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
    sentiment      DOUBLE PRECISION NOT NULL DEFAULT 0 CHECK (sentiment >= -1 AND sentiment <= 1),
    pause_trading  BOOLEAN NOT NULL DEFAULT FALSE,
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

-- =============================================================================
-- LIVE browser-execution path (only used when LIVE_BROWSER_TRADING_ENABLED=on)
-- =============================================================================

-- Order queue: the bridge inserts gate-approved orders; the browser subagent
-- claims and executes them on the live BingX web UI.
CREATE TABLE IF NOT EXISTS execution_orders (
    id          BIGSERIAL PRIMARY KEY,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    claimed_at  TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','claimed','done','failed','denied')),
    action      TEXT NOT NULL CHECK (action IN ('enter','exit')),
    pair        TEXT NOT NULL,
    side        TEXT NOT NULL DEFAULT 'long',
    order_type  TEXT NOT NULL DEFAULT 'market' CHECK (order_type IN ('market','limit')),
    stake       DOUBLE PRECISION NOT NULL DEFAULT 0,
    amount      DOUBLE PRECISION,
    price       DOUBLE PRECISION,
    tag         TEXT NOT NULL DEFAULT '',
    detail      TEXT NOT NULL DEFAULT '',
    -- Rich signal fields for the Risk Governor (stop/take-profit/atr/etc.).
    meta        JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_execution_orders_pending
    ON execution_orders (id) WHERE status = 'pending';

-- Key/value flags: kill switch + latest real-account snapshot.
CREATE TABLE IF NOT EXISTS system_flags (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Default kill switch to OFF (running). Fail-closed reads still treat a missing
-- row / unreachable DB as tripped.
INSERT INTO system_flags (key, value, reason)
VALUES ('kill_switch', 'off', 'init')
ON CONFLICT (key) DO NOTHING;
