-- ============================================================
-- Store Intelligence — SQLite schema
-- ============================================================
-- Design notes:
--  • SQLite is chosen for portability inside a single docker-compose.
--    For a production rollout to 40 stores, this would move to Postgres
--    (see docs/CHOICES.md).
--  • All timestamps are stored as ISO-8601 strings in UTC. SQLite has
--    no native datetime type; storing as TEXT keeps round-trips lossless
--    and lets us use ORDER BY / range scans directly on the column.
--  • metadata / coordinates / open_hours are stored as JSON TEXT and
--    parsed at the application boundary. SQLite 3.38+ has json1 builtin.
-- ============================================================

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;        -- concurrent reads while ingest writes
PRAGMA synchronous = NORMAL;      -- WAL + NORMAL = safe + fast for our workload

-- ------------------------------------------------------------
-- stores: master record for each physical store
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stores (
    store_id     TEXT PRIMARY KEY,            -- e.g. STORE_BLR_002
    name         TEXT NOT NULL,
    timezone     TEXT NOT NULL DEFAULT 'Asia/Kolkata',
    open_hours   TEXT NOT NULL                -- JSON: {"mon":["10:00","21:00"], ...}
);

-- ------------------------------------------------------------
-- zones: named regions within a store, scoped to a camera
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS zones (
    zone_id      TEXT NOT NULL,               -- e.g. SKINCARE
    store_id     TEXT NOT NULL,
    name         TEXT NOT NULL,
    camera_id    TEXT NOT NULL,               -- which camera covers it
    coordinates  TEXT NOT NULL,               -- JSON polygon: [[x,y],...]
    PRIMARY KEY (store_id, zone_id),
    FOREIGN KEY (store_id) REFERENCES stores(store_id) ON DELETE CASCADE
);

-- ------------------------------------------------------------
-- events: append-only stream of detection events
--   • event_id is the dedup key (idempotent ingest)
--   • metadata is JSON: queue_depth, sku_zone, session_seq
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS events (
    event_id     TEXT PRIMARY KEY,            -- UUID v4 from detector
    store_id     TEXT NOT NULL,
    camera_id    TEXT NOT NULL,
    visitor_id   TEXT NOT NULL,
    event_type   TEXT NOT NULL,               -- ENTRY|EXIT|ZONE_*|BILLING_*|REENTRY
    timestamp    TEXT NOT NULL,               -- ISO-8601 UTC
    zone_id      TEXT,                        -- NULL for ENTRY/EXIT
    dwell_ms     INTEGER NOT NULL DEFAULT 0,
    is_staff     INTEGER NOT NULL DEFAULT 0,  -- SQLite has no bool
    confidence   REAL NOT NULL,               -- 0.0..1.0
    metadata     TEXT NOT NULL DEFAULT '{}',  -- JSON
    ingested_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CHECK (confidence >= 0.0 AND confidence <= 1.0),
    CHECK (is_staff IN (0,1)),
    CHECK (event_type IN (
        'ENTRY','EXIT','ZONE_ENTER','ZONE_EXIT','ZONE_DWELL',
        'BILLING_QUEUE_JOIN','BILLING_QUEUE_ABANDON','REENTRY'
    ))
);

-- Indexes on events --------------------------------------------------------
-- store_id: every analytics query is scoped to a store.
CREATE INDEX IF NOT EXISTS idx_events_store        ON events(store_id);
-- visitor_id: funnel + session reconstruction walk events for a visitor.
CREATE INDEX IF NOT EXISTS idx_events_visitor      ON events(visitor_id);
-- timestamp: range scans for "today", "last 30 min", anomaly windows.
CREATE INDEX IF NOT EXISTS idx_events_ts           ON events(timestamp);
-- event_type: counts of ENTRY / BILLING_QUEUE_JOIN etc.
CREATE INDEX IF NOT EXISTS idx_events_type         ON events(event_type);
-- composite: the hot path is "events for store X in time window".
CREATE INDEX IF NOT EXISTS idx_events_store_ts     ON events(store_id, timestamp);
-- composite: per-store, per-type scans (funnel stages, queue counts).
CREATE INDEX IF NOT EXISTS idx_events_store_type   ON events(store_id, event_type);
-- camera staleness check.
CREATE INDEX IF NOT EXISTS idx_events_store_cam_ts ON events(store_id, camera_id, timestamp);

-- ------------------------------------------------------------
-- visitor_sessions: derived per-visit record
--   Materialised on ingest of ENTRY/EXIT to keep funnel queries cheap.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS visitor_sessions (
    session_id     TEXT PRIMARY KEY,          -- UUID
    visitor_id     TEXT NOT NULL,
    store_id       TEXT NOT NULL,
    entry_time     TEXT NOT NULL,
    exit_time      TEXT,                      -- NULL while in store
    is_converted   INTEGER NOT NULL DEFAULT 0,-- POS-correlated
    reentry_count  INTEGER NOT NULL DEFAULT 0,
    is_staff       INTEGER NOT NULL DEFAULT 0,
    CHECK (is_converted IN (0,1)),
    CHECK (is_staff IN (0,1))
);

CREATE INDEX IF NOT EXISTS idx_sessions_store    ON visitor_sessions(store_id);
CREATE INDEX IF NOT EXISTS idx_sessions_visitor  ON visitor_sessions(visitor_id);
CREATE INDEX IF NOT EXISTS idx_sessions_entry    ON visitor_sessions(entry_time);
CREATE INDEX IF NOT EXISTS idx_sessions_store_entry ON visitor_sessions(store_id, entry_time);

-- ------------------------------------------------------------
-- transactions: POS records, time-correlated to sessions
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS transactions (
    transaction_id    TEXT PRIMARY KEY,
    store_id          TEXT NOT NULL,
    timestamp         TEXT NOT NULL,
    basket_value_inr  REAL NOT NULL,
    FOREIGN KEY (store_id) REFERENCES stores(store_id)
);

CREATE INDEX IF NOT EXISTS idx_txn_store    ON transactions(store_id);
CREATE INDEX IF NOT EXISTS idx_txn_ts       ON transactions(timestamp);
CREATE INDEX IF NOT EXISTS idx_txn_store_ts ON transactions(store_id, timestamp);

-- ------------------------------------------------------------
-- anomalies: detected operational anomalies
--   Stored so we can show "active" and have a history.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS anomalies (
    anomaly_id        TEXT PRIMARY KEY,
    store_id          TEXT NOT NULL,
    type              TEXT NOT NULL,          -- QUEUE_SPIKE|CONVERSION_DROP|DEAD_ZONE|STALE_CAMERA
    severity          TEXT NOT NULL,          -- INFO|WARN|CRITICAL
    description       TEXT NOT NULL,
    suggested_action  TEXT NOT NULL,
    detected_at       TEXT NOT NULL,
    resolved_at       TEXT,                   -- NULL = active
    CHECK (severity IN ('INFO','WARN','CRITICAL')),
    CHECK (type IN ('QUEUE_SPIKE','CONVERSION_DROP','DEAD_ZONE','STALE_CAMERA'))
);

CREATE INDEX IF NOT EXISTS idx_anom_store      ON anomalies(store_id);
CREATE INDEX IF NOT EXISTS idx_anom_active     ON anomalies(store_id, resolved_at);
CREATE INDEX IF NOT EXISTS idx_anom_detected   ON anomalies(detected_at);
