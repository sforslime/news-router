import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db, normalize as n
from app.config import TEST_DATABASE_URL

# Tables the storage fixture rebuilds. Dropped newest-first so the foreign keys
# come apart in order.
_TABLES = "cluster_gists, article_revisions, articles, clusters, api_keys, sources"


@pytest.fixture
def conn():
    """A clean Postgres schema per test.

    Storage tests need a real Postgres — the schema uses a generated tsvector
    column and Postgres text search, so faking it would test something the
    application does not run. Point TEST_DATABASE_URL at a scratch database
    (a Neon branch works well) to enable them.
    """
    if not TEST_DATABASE_URL:
        pytest.skip("set TEST_DATABASE_URL to run storage tests")
    c = db.connect(TEST_DATABASE_URL)
    c.execute(f"DROP TABLE IF EXISTS {_TABLES} CASCADE")
    db.init_db(c)
    db.sync_sources(c)
    yield c
    c.close()


def make_record(**overrides):
    rec = {
        "id": "premium-times:1", "source_id": "premium-times", "source_article_id": "1",
        "headline": "Minister resigns over contract scandal", "dek": "The minister stepped down on Friday.",
        "byline": "A Reporter", "published_at": "2026-08-21T10:00:00Z",
        "published_at_reported": "2026-08-21T11:00:00", "updated_at": "2026-08-21T10:00:00Z",
        "first_seen_at": "2026-08-21T10:05:00Z",
        "canonical_url": "https://www.premiumtimesng.com/news/1-minister-resigns.html",
        "section": "Headline Stories", "snippet": "The minister stepped down on Friday after a report.",
        "image": None, "language": "en", "wire_source": None, "paywalled": 0, "sponsored": 0,
        "entities": json.dumps(["Bola Tinubu"]),
    }
    rec.update(overrides)
    rec["content_hash"] = n.content_hash(rec["headline"], rec["dek"], rec["snippet"])
    return rec
