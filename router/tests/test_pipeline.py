import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db, normalize as n
from app.serialize import article_out


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init_db(c)
    db.sync_sources(c)
    return c


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


class TestNormalize:
    def test_wat_is_converted_to_utc(self):
        # WordPress date_gmt carries no offset but is UTC.
        assert n.to_utc_iso("2026-08-21T19:39:34") == "2026-08-21T19:39:34Z"

    def test_duplicated_excerpt_is_collapsed(self):
        one = "The phoney agency was operating within the Office of the Secretary."
        assert n.collapse_duplicate(one + one) == one

    def test_genuine_repetition_is_not_mangled(self):
        text = "Fifty people died. Rescue teams continue. Fifty people remain missing today."
        assert n.collapse_duplicate(text) == text

    def test_wire_copy_is_detected(self):
        assert n.detect_wire("ABUJA, Aug 21 (NAN) The president said") == "NAN"
        assert n.detect_wire("Our correspondent reports that") is None

    def test_language_defaults_to_english_without_strong_signal(self):
        assert n.detect_language("Tinubu suspends three permanent secretaries") == "en"
        assert n.detect_language("Gwamnati jihar Kano sun kuma cikin wanda mutane") == "ha"

    def test_sponsored_content_is_flagged(self):
        assert n.is_sponsored("Promoted Stories", "Press Release") is True
        assert n.is_sponsored("Headline Stories", "Yakubu Mohammed") is False

    def test_truncate_breaks_on_word_boundary(self):
        assert n.truncate("the quick brown fox jumps", 12) == "the quick…"


class TestRevisions:
    def test_first_ingest_creates_revision_one(self, conn):
        assert db.upsert_article(conn, make_record()) == "new"
        rows = conn.execute("SELECT * FROM article_revisions").fetchall()
        assert len(rows) == 1 and rows[0]["revision"] == 1

    def test_reingesting_unchanged_article_is_a_noop(self, conn):
        db.upsert_article(conn, make_record())
        assert db.upsert_article(conn, make_record()) == "unchanged"
        assert conn.execute("SELECT COUNT(*) c FROM article_revisions").fetchone()["c"] == 1

    def test_correction_creates_a_revision_and_names_changed_fields(self, conn):
        db.upsert_article(conn, make_record())
        corrected = make_record(headline="Minister resigns over procurement scandal")
        assert db.upsert_article(conn, corrected) == "updated"

        article = conn.execute("SELECT * FROM articles WHERE id='premium-times:1'").fetchone()
        assert article["revision"] == 2
        assert article["headline"] == "Minister resigns over procurement scandal"

        latest = conn.execute(
            "SELECT * FROM article_revisions WHERE revision=2"
        ).fetchone()
        assert json.loads(latest["changed"]) == ["headline"]

    def test_first_seen_survives_a_publisher_edit(self, conn):
        db.upsert_article(conn, make_record())
        db.upsert_article(conn, make_record(headline="Rewritten", first_seen_at="2027-01-01T00:00:00Z"))
        article = conn.execute("SELECT * FROM articles WHERE id='premium-times:1'").fetchone()
        # A publisher must not be able to move the timestamp we recorded.
        assert article["first_seen_at"] == "2026-08-21T10:05:00Z"

    def test_silent_edit_is_caught_even_when_modified_is_unchanged(self, conn):
        db.upsert_article(conn, make_record())
        # Same updated_at, different body-derived hash.
        sneaky = make_record(snippet="The minister denies any wrongdoing whatsoever.")
        assert db.upsert_article(conn, sneaky) == "updated"


class TestRights:
    def test_unlicensed_source_has_dek_and_image_withheld(self, conn):
        db.upsert_article(conn, make_record())
        article = conn.execute("SELECT * FROM articles WHERE id='premium-times:1'").fetchone()
        punch = conn.execute("SELECT * FROM sources WHERE id='punch'").fetchone()

        out = article_out(article, punch)
        assert out["dek"] is None and out["snippet"] is None
        assert out["rights"]["snippet"] is False

    def test_licensed_source_returns_the_granted_fields(self, conn):
        db.upsert_article(conn, make_record())
        article = conn.execute("SELECT * FROM articles WHERE id='premium-times:1'").fetchone()
        pt = conn.execute("SELECT * FROM sources WHERE id='premium-times'").fetchone()

        out = article_out(article, pt)
        assert out["dek"] == "The minister stepped down on Friday."
        assert out["rights"]["body"] is False  # never licensed at any tier

    def test_body_is_never_stored(self, conn):
        db.upsert_article(conn, make_record())
        cols = {r[1] for r in conn.execute("PRAGMA table_info(articles)")}
        assert "body" not in cols and "content" not in cols


class TestVolatileMarkup:
    """Some themes regenerate ad-container ids on every request. Those must not
    reach the hash, or every article looks edited on every ingest."""

    def _post(self, marker: str) -> dict:
        return {
            "id": 1,
            "title": {"rendered": "NCAA orders arrest after airport incident"},
            "excerpt": {"rendered": "The aviation authority acted on Monday."},
            "content": {"rendered": (
                "<p>The Nigeria Civil Aviation Authority acted on Monday. " +
                ("The bus was left on the runway apron for several minutes. " * 12) +
                "</p>"
                f'<div class="td-a-rec"><script>var slot = "{marker}";</script></div>'
                "<p>The driver has not been named.</p>"
            )},
            "date_gmt": "2026-08-21T10:00:00",
            "date": "2026-08-21T11:00:00",
            "modified_gmt": "2026-08-21T10:00:00",
            "link": "https://ripplesnigeria.com/story",
        }

    def test_script_bodies_do_not_reach_the_text(self):
        text = n.strip_html(self._post("pnbwh3d8")["content"]["rendered"])
        assert "var slot" not in text
        assert "Civil Aviation Authority" in text

    def test_hash_survives_a_regenerated_ad_id(self):
        a = n.normalize_wordpress(self._post("pnbwh3d8"), "ripples")
        b = n.normalize_wordpress(self._post("fyrmu52b"), "ripples")
        assert a["content_hash"] == b["content_hash"]

    def test_a_body_rewrite_is_named_in_the_revision(self, conn):
        rec = n.normalize_wordpress(self._post("x"), "ripples")
        assert db.upsert_article(conn, rec) == "new"
        edited = self._post("x")
        # Appended past the snippet window, so only the body moves.
        edited["content"]["rendered"] += "<p>A correction was appended at the foot.</p>"
        rec2 = n.normalize_wordpress(edited, "ripples")
        assert db.upsert_article(conn, rec2) == "updated"
        row = conn.execute(
            "SELECT changed FROM article_revisions WHERE article_id=? AND revision=2",
            (rec2["id"],),
        ).fetchone()
        assert json.loads(row["changed"]) == ["body"]
