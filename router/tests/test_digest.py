"""Clustering and gist generation. Matching rules are pure functions pinned
directly; storage behaviour runs against the same scratch Postgres as the
pipeline tests. No test talks to the Claude API — the client is stubbed."""
import json
from datetime import datetime, timedelta, timezone

from app import cluster, gist
from conftest import make_record


def _iso(hours_ago: float) -> str:
    dt = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return dt.isoformat().replace("+00:00", "Z")


def _story(headline, source_id="premium-times", entities=(), wire=None):
    return cluster._story({
        "id": f"{source_id}:x", "source_id": source_id, "headline": headline,
        "entities": json.dumps(list(entities)), "wire_source": wire,
        "published_at": _iso(1), "cluster_id": None,
    })


class TestMatchingRule:
    def test_same_event_under_different_headlines_matches(self):
        a = _story("NLC general secretary Emmanuel Ugboaja dies at 60")
        b = _story("BREAKING: NLC general secretary Ugboaja is dead", source_id="punch")
        assert cluster.same_story(a, b)

    def test_sharing_only_a_name_is_not_a_story(self):
        a = _story("Tinubu appoints new NNPC board chairman")
        b = _story("Tinubu departs Abuja for France summit", source_id="punch")
        assert not cluster.same_story(a, b)

    def test_stopwords_carry_no_weight(self):
        # 'nigeria', 'says', 'new' are in every third headline.
        a = _story("Nigeria says new policy will boost farming")
        b = _story("Nigeria says new stadium will open soon", source_id="punch")
        assert not cluster.same_story(a, b)

    def test_two_shared_entity_tags_match_on_their_own(self):
        a = _story("Anti-graft agency quizzes ex-governor", entities=["ICPC", "Bola Tinubu"])
        b = _story("Ex-governor faces fresh questioning", source_id="punch",
                   entities=["Bola Tinubu", "ICPC"])
        assert cluster.same_story(a, b)

    def test_wire_copy_matches_on_half_shared_words(self):
        a = _story("Gombe United appoint Dombraye as technical adviser", wire="NAN")
        b = _story("Gombe United name Dombraye technical adviser", source_id="punch", wire="NAN")
        assert cluster.same_story(a, b)

    def test_entity_words_count_toward_headline_overlap(self):
        # Punch's feed has no tags; the other side's tags still help by
        # contributing their words to the comparison.
        a = _story("Court adjourns arraignment", entities=["Emmanuel Ugboaja", "NLC"])
        b = _story("Court adjourns arraignment of NLC's Ugboaja", source_id="punch")
        assert cluster.same_story(a, b)


class TestClusteringStorage:
    def _seed(self, conn):
        db_rows = [
            make_record(
                id="premium-times:10", source_id="premium-times", source_article_id="10",
                headline="NLC general secretary Emmanuel Ugboaja dies at 60",
                entities=json.dumps(["Emmanuel Ugboaja", "NLC"]), published_at=_iso(3),
            ),
            make_record(
                id="punch:20", source_id="punch", source_article_id="20",
                headline="BREAKING: NLC general secretary Ugboaja is dead",
                entities="[]", published_at=_iso(2),
            ),
            make_record(
                id="ripples:30", source_id="ripples", source_article_id="30",
                headline="Dangote refinery cuts petrol price again",
                entities="[]", published_at=_iso(2),
            ),
        ]
        from app import db as db_mod
        for rec in db_rows:
            db_mod.upsert_article(conn, rec)

    def test_same_story_across_outlets_forms_a_cluster(self, conn):
        self._seed(conn)
        stats = cluster.run(conn)
        assert stats["assigned"] == 2 and stats["clusters_touched"] == 1

        rows = conn.execute(
            "SELECT id, cluster_id FROM articles ORDER BY id"
        ).fetchall()
        by_id = {r["id"]: r["cluster_id"] for r in rows}
        assert by_id["premium-times:10"] is not None
        assert by_id["premium-times:10"] == by_id["punch:20"]
        assert by_id["ripples:30"] is None  # unrelated stays a singleton, no cluster row

        c = conn.execute("SELECT * FROM clusters").fetchall()
        assert len(c) == 1 and c[0]["size"] == 2
        assert c[0]["label"] == "NLC general secretary Emmanuel Ugboaja dies at 60"
        assert c[0]["lead_article_id"] == "premium-times:10"

    def test_rerun_reassigns_nothing(self, conn):
        self._seed(conn)
        cluster.run(conn)
        before = {r["id"]: r["cluster_id"] for r in conn.execute("SELECT id, cluster_id FROM articles")}
        stats = cluster.run(conn)
        after = {r["id"]: r["cluster_id"] for r in conn.execute("SELECT id, cluster_id FROM articles")}
        assert stats["assigned"] == 0 and before == after

    def test_sponsored_copy_is_never_clustered(self, conn):
        from app import db as db_mod
        db_mod.upsert_article(conn, make_record(
            id="premium-times:11", source_id="premium-times", source_article_id="11",
            headline="Brand X launches wealth summit in Lagos", sponsored=1, published_at=_iso(2),
        ))
        db_mod.upsert_article(conn, make_record(
            id="punch:21", source_id="punch", source_article_id="21",
            headline="Brand X launches wealth summit in Lagos", sponsored=1, published_at=_iso(1),
        ))
        stats = cluster.run(conn)
        assert stats["assigned"] == 0

    def test_same_outlet_alone_never_forms_a_cluster(self, conn):
        from app import db as db_mod
        db_mod.upsert_article(conn, make_record(
            id="premium-times:12", source_id="premium-times", source_article_id="12",
            headline="Senate passes electoral bill after long debate", published_at=_iso(2),
        ))
        db_mod.upsert_article(conn, make_record(
            id="premium-times:13", source_id="premium-times", source_article_id="13",
            headline="Senate passes electoral bill, opposition objects", published_at=_iso(1),
        ))
        stats = cluster.run(conn)
        assert stats["assigned"] == 0


class _FakeMessages:
    def __init__(self, log):
        self._log = log

    def parse(self, **kwargs):
        self._log.append(kwargs)

        class _Response:
            stop_reason = "end_turn"
            parsed_output = gist.Gist(
                summary="The NLC general secretary died at 60.",
                coverage=[gist.OutletNote(source_id="premium-times", note="First to report, with entities."),
                          gist.OutletNote(source_id="punch", note="Breaking treatment.")],
            )

        return _Response()


class _FakeAnthropic:
    calls: list = []

    def __init__(self, api_key=None):
        self.messages = _FakeMessages(self.calls)


class TestGist:
    def test_prompt_respects_each_outlets_rights(self):
        sources = {
            "premium-times": {"id": "premium-times", "attribution_name": "Premium Times",
                              "rights_dek": 1, "rights_snippet": 1},
            "punch": {"id": "punch", "attribution_name": "Punch",
                      "rights_dek": 0, "rights_snippet": 0},
        }
        articles = [
            make_record(dek="A licensed description.", snippet="A licensed snippet too."),
            make_record(id="punch:2", source_id="punch",
                        headline="Minister quits amid contract row",
                        dek="An unlicensed description.", snippet="An unlicensed snippet."),
        ]
        prompt = gist.build_prompt({"label": "Minister resigns"}, articles, sources)
        assert "A licensed description." in prompt
        assert "Minister quits amid contract row" in prompt  # headline always allowed
        assert "unlicensed" not in prompt                    # dek and snippet withheld

    def test_input_hash_tracks_membership_and_content(self):
        a, b = make_record(), make_record(id="punch:2", source_id="punch")
        same = gist.input_hash([a, b])
        assert gist.input_hash([b, a]) == same            # order-free
        assert gist.input_hash([a]) != same               # membership moves it
        edited = dict(b, content_hash="different")
        assert gist.input_hash([a, edited]) != same       # a silent edit moves it

    def test_unset_key_skips_cleanly(self, monkeypatch):
        monkeypatch.setattr(gist, "ANTHROPIC_API_KEY", "")
        stats = gist.generate(None)
        assert stats["status"] == "not configured"

    def test_generate_writes_once_and_skips_unchanged(self, conn, monkeypatch):
        from app import db as db_mod
        db_mod.upsert_article(conn, make_record(
            id="premium-times:10", source_id="premium-times", source_article_id="10",
            headline="NLC general secretary Emmanuel Ugboaja dies at 60",
            entities=json.dumps(["Emmanuel Ugboaja", "NLC"]), published_at=_iso(3)))
        db_mod.upsert_article(conn, make_record(
            id="punch:20", source_id="punch", source_article_id="20",
            headline="BREAKING: NLC general secretary Ugboaja is dead",
            entities="[]", published_at=_iso(2)))
        cluster.run(conn)

        monkeypatch.setattr(gist, "ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setattr(gist.anthropic, "Anthropic", _FakeAnthropic)
        _FakeAnthropic.calls.clear()

        stats = gist.generate(conn)
        assert stats == {"status": "ok", "generated": 1, "unchanged": 0, "errors": []}
        assert len(_FakeAnthropic.calls) == 1

        row = conn.execute("SELECT * FROM cluster_gists").fetchone()
        assert row["summary"] == "The NLC general secretary died at 60."
        assert json.loads(row["coverage"])[0]["source_id"] == "premium-times"

        # Nothing changed, so the second run costs nothing.
        stats = gist.generate(conn)
        assert stats["generated"] == 0 and stats["unchanged"] == 1
        assert len(_FakeAnthropic.calls) == 1


class TestEntityFiller:
    def test_branding_filler_tags_are_not_entities(self):
        # Peoples Gazette tags every article ["news", "Nigeria", "Nigerian news",
        # ...]; Premium Times adds ["News", "Nigeria", ...]. Two unrelated
        # articles must not match on that boilerplate.
        a = _story("Otedola buys 95.7 million First HoldCo shares",
                   entities=["News", "Nigeria", "Otedola", "NGX"])
        b = _story("Suspected vandal arrested in Bauchi", source_id="peoples-gazette",
                   entities=["news", "Nigeria", "Nigerian news", "Peoples Gazette"])
        assert not cluster.same_story(a, b)
