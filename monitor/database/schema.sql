-- Listings seen on any source. `dedupe_key` is the site's own listing id
-- when available, otherwise a hash of the canonical URL.
CREATE TABLE IF NOT EXISTS listings (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key           TEXT    NOT NULL UNIQUE,
    source               TEXT    NOT NULL,
    external_id          TEXT    NOT NULL,
    url                  TEXT    NOT NULL,
    title                TEXT,
    property_type        TEXT,
    operation            TEXT,
    city                 TEXT,
    neighborhood         TEXT,
    raw_neighborhood     TEXT,
    address              TEXT,
    price                REAL,
    currency             TEXT,
    area_m2              REAL,
    rooms                INTEGER,
    bedrooms             INTEGER,
    furnished            INTEGER,
    expensas             REAL,
    expensas_currency    TEXT,
    published_at         TEXT,
    price_is_approximate INTEGER NOT NULL DEFAULT 0,
    matched              INTEGER NOT NULL DEFAULT 0,
    reject_reason        TEXT,
    first_seen_at        TEXT    NOT NULL,
    last_seen_at         TEXT    NOT NULL,
    notified_at          TEXT,
    notify_attempts      INTEGER NOT NULL DEFAULT 0,
    extra                TEXT
);

CREATE INDEX IF NOT EXISTS idx_listings_source ON listings (source);
CREATE INDEX IF NOT EXISTS idx_listings_pending ON listings (matched, notified_at);
CREATE INDEX IF NOT EXISTS idx_listings_first_seen ON listings (first_seen_at);

-- One row per monitoring cycle, for /status.
CREATE TABLE IF NOT EXISTS cycles (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    sources_total  INTEGER NOT NULL DEFAULT 0,
    sources_ok     INTEGER NOT NULL DEFAULT 0,
    found          INTEGER NOT NULL DEFAULT 0,
    matched        INTEGER NOT NULL DEFAULT 0,
    new_listings   INTEGER NOT NULL DEFAULT 0,
    notified       INTEGER NOT NULL DEFAULT 0,
    errors         TEXT
);

-- Per-source health, so a blocked source can be backed off and reported.
CREATE TABLE IF NOT EXISTS source_state (
    source                TEXT PRIMARY KEY,
    last_attempt_at       TEXT,
    last_success_at       TEXT,
    last_status           TEXT,
    last_error            TEXT,
    consecutive_failures  INTEGER NOT NULL DEFAULT 0,
    blocked_until         TEXT,
    total_found           INTEGER NOT NULL DEFAULT 0,
    total_new             INTEGER NOT NULL DEFAULT 0
);

-- Small key/value store: Telegram update offset, bootstrap flag, ...
CREATE TABLE IF NOT EXISTS app_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Telegram chats that sent /start. `active` drops to 0 on /stop or when the
-- user blocks the bot; the row is kept so a later /start just reactivates it.
CREATE TABLE IF NOT EXISTS subscribers (
    chat_id        TEXT PRIMARY KEY,
    username       TEXT,
    subscribed_at  TEXT    NOT NULL,
    active         INTEGER NOT NULL DEFAULT 1
);

-- A subscriber's own search, as JSON (SearchCriteria.to_dict). No row means
-- the default search from the configuration.
CREATE TABLE IF NOT EXISTS subscriber_criteria (
    chat_id     TEXT PRIMARY KEY,
    criteria    TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Which listing went to which chat, so each subscriber gets a listing once
-- and a failed delivery to one chat can be retried without resending it to
-- the others.
CREATE TABLE IF NOT EXISTS deliveries (
    chat_id     TEXT NOT NULL,
    dedupe_key  TEXT NOT NULL,
    sent_at     TEXT NOT NULL,
    PRIMARY KEY (chat_id, dedupe_key)
);
