-- News Router storage schema (PostgreSQL).
--
-- Two deliberate departures from idiomatic Postgres, both to keep this schema
-- honest about what the data actually is:
--
-- 1. Timestamps are TEXT holding ISO-8601 UTC strings, not TIMESTAMPTZ. The
--    router receives times it does not trust — Nigerian WordPress backdates,
--    mixes WAT with UTC, and revises timestamps after publication. Storing the
--    exact string received keeps that visible instead of laundering it through
--    a type conversion. ISO-8601 UTC sorts correctly as text, so ordering,
--    range filters and keyset pagination all behave.
--
-- 2. Flags are INTEGER 0/1 rather than BOOLEAN, so that comparisons written as
--    `enabled = 1` mean the same thing here as they did on SQLite.

CREATE TABLE IF NOT EXISTS sources (
  id                TEXT PRIMARY KEY,          -- slug, e.g. 'premium-times'
  name              TEXT NOT NULL,
  homepage          TEXT NOT NULL,
  tier              INTEGER NOT NULL,          -- 1 installed API, 2 wp-json, 3 gated, 4 rss
  adapter           TEXT NOT NULL,             -- wordpress | content_api | rss
  endpoint          TEXT NOT NULL,
  enabled           INTEGER NOT NULL DEFAULT 0,
  -- Licensing. Nothing is served to clients that the rights columns do not allow.
  license_status    TEXT NOT NULL DEFAULT 'none',   -- signed | verbal | pending | none
  rights_dek        INTEGER NOT NULL DEFAULT 0,
  rights_snippet    INTEGER NOT NULL DEFAULT 0,
  rights_image      INTEGER NOT NULL DEFAULT 0,
  attribution_name  TEXT,
  timezone          TEXT NOT NULL DEFAULT 'Africa/Lagos',
  added_at          TEXT NOT NULL,
  last_ingest_at    TEXT,
  last_error        TEXT
);

CREATE TABLE IF NOT EXISTS articles (
  id                    TEXT PRIMARY KEY,      -- '{source_id}:{source_article_id}'
  source_id             TEXT NOT NULL REFERENCES sources(id),
  source_article_id     TEXT NOT NULL,
  headline              TEXT NOT NULL,
  dek                   TEXT,
  byline                TEXT,
  -- published_at is normalised UTC. published_at_reported keeps whatever the
  -- site claimed, because the two disagree constantly.
  published_at          TEXT NOT NULL,
  published_at_reported TEXT,
  updated_at            TEXT,
  first_seen_at         TEXT NOT NULL,         -- when the router saw it; always trustworthy
  canonical_url         TEXT NOT NULL,
  section               TEXT,
  snippet               TEXT,
  image                 TEXT,
  language              TEXT NOT NULL DEFAULT 'en',
  wire_source           TEXT,                  -- NAN | Reuters | AFP | NULL (own reporting)
  paywalled             INTEGER NOT NULL DEFAULT 0,
  sponsored             INTEGER NOT NULL DEFAULT 0,  -- advertorial / syndicated PR
  content_hash          TEXT NOT NULL,         -- detects silent edits updated_at misses
  entities              TEXT NOT NULL DEFAULT '[]',  -- JSON array, from publisher tags
  revision              INTEGER NOT NULL DEFAULT 1,
  retracted             INTEGER NOT NULL DEFAULT 0,
  retraction_note       TEXT,
  cluster_id            TEXT,
  -- Full-text index, maintained by Postgres rather than by the application.
  -- Weighted so a term in the headline outranks the same term in a snippet.
  search                tsvector GENERATED ALWAYS AS (
                          setweight(to_tsvector('english', coalesce(headline, '')), 'A') ||
                          setweight(to_tsvector('english', coalesce(dek, '')), 'B') ||
                          setweight(to_tsvector('english', coalesce(snippet, '')), 'C') ||
                          setweight(to_tsvector('english', coalesce(entities, '')), 'D')
                        ) STORED,
  UNIQUE (source_id, source_article_id)
);

CREATE INDEX IF NOT EXISTS idx_articles_published  ON articles(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_articles_source     ON articles(source_id, published_at DESC);
CREATE INDEX IF NOT EXISTS idx_articles_cluster    ON articles(cluster_id);
CREATE INDEX IF NOT EXISTS idx_articles_hash       ON articles(content_hash);
CREATE INDEX IF NOT EXISTS idx_articles_firstseen  ON articles(first_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_articles_search     ON articles USING GIN (search);

-- Every observed change to a story. This is what makes corrections and
-- retractions propagate, and what exposes stealth edits.
CREATE TABLE IF NOT EXISTS article_revisions (
  article_id    TEXT NOT NULL REFERENCES articles(id),
  revision      INTEGER NOT NULL,
  content_hash  TEXT NOT NULL,
  headline      TEXT NOT NULL,
  dek           TEXT,
  snippet       TEXT,
  seen_at       TEXT NOT NULL,
  changed       TEXT NOT NULL DEFAULT '[]',    -- JSON array of field names
  PRIMARY KEY (article_id, revision)
);

CREATE TABLE IF NOT EXISTS clusters (
  id                 TEXT PRIMARY KEY,
  label              TEXT,
  lead_article_id    TEXT,
  size               INTEGER NOT NULL DEFAULT 1,
  first_published_at TEXT,
  last_published_at  TEXT,
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_keys (
  key_hash      TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  plan          TEXT NOT NULL DEFAULT 'free',
  rate_per_min  INTEGER NOT NULL DEFAULT 60,
  enabled       INTEGER NOT NULL DEFAULT 1,
  created_at    TEXT NOT NULL
);

-- One generated gist per story cluster: a neutral summary plus a per-outlet
-- note, written by a model from licensed metadata only (never bodies), and
-- regenerated when input_hash says the underlying coverage moved.
CREATE TABLE IF NOT EXISTS cluster_gists (
  cluster_id    TEXT PRIMARY KEY REFERENCES clusters(id),
  summary       TEXT NOT NULL,
  coverage      TEXT NOT NULL DEFAULT '[]',  -- JSON: [{source_id, note}]
  model         TEXT NOT NULL,
  input_hash    TEXT NOT NULL,               -- hash of member ids + content hashes
  generated_at  TEXT NOT NULL
);

-- Small pipeline facts the serving path needs to explain itself — e.g. why a
-- cluster has no gist (the writer may live on a laptop that was offline).
CREATE TABLE IF NOT EXISTS pipeline_state (
  key         TEXT PRIMARY KEY,
  value       TEXT NOT NULL,     -- JSON
  updated_at  TEXT NOT NULL
);
