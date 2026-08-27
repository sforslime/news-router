"""The on-demand topic gist: which articles it reads, what it streams, and how
it degrades when there is nothing to write with. No test talks to a model."""
import json
from datetime import datetime, timedelta, timezone

from app import gist
from app.routes.search import MAX_TOPIC_ARTICLES, _gist_cache, _gist_lines, _topic_rows
from conftest import make_record


def _iso(days_ago: float) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return dt.isoformat().replace("+00:00", "Z")


def _events(q, rows, sources, days=7):
    return [json.loads(line) for line in _gist_lines(q, days, rows, sources)]


class TestTopicRows:
    def _seed(self, conn):
        from app import db as db_mod
        recs = [
            make_record(id="premium-times:1", source_article_id="1",
                        headline="Osun election tribunal fixes ruling date", published_at=_iso(1)),
            make_record(id="punch:2", source_id="punch", source_article_id="2",
                        headline="INEC releases Osun election timetable", published_at=_iso(3)),
            make_record(id="ripples:3", source_id="ripples", source_article_id="3",
                        headline="Osun election violence victims get N500m", published_at=_iso(20)),
            make_record(id="punch:4", source_id="punch", source_article_id="4",
                        headline="Sponsored: win big in the Osun election raffle",
                        sponsored=1, published_at=_iso(1)),
        ]
        for r in recs:
            db_mod.upsert_article(conn, r)

    def test_window_and_sponsored_filters(self, conn):
        self._seed(conn)
        ids = {r["id"] for r in _topic_rows(conn, "osun election", days=7)}
        assert ids == {"premium-times:1", "punch:2"}  # old and advertorial excluded

    def test_wider_window_reaches_older_coverage(self, conn):
        self._seed(conn)
        ids = {r["id"] for r in _topic_rows(conn, "osun election", days=30)}
        assert "ripples:3" in ids

    def test_cap_is_respected(self, conn):
        from app import db as db_mod
        for i in range(MAX_TOPIC_ARTICLES + 5):
            db_mod.upsert_article(conn, make_record(
                id=f"premium-times:{i}", source_article_id=str(i),
                headline=f"Osun election update number {i}", published_at=_iso(1)))
        assert len(_topic_rows(conn, "osun election", days=7)) == MAX_TOPIC_ARTICLES


class TestTopicPrompt:
    def test_rights_gate_matches_the_serving_path(self):
        sources = {
            "premium-times": {"id": "premium-times", "attribution_name": "Premium Times",
                              "rights_dek": 1, "rights_snippet": 1},
            "punch": {"id": "punch", "attribution_name": "Punch",
                      "rights_dek": 0, "rights_snippet": 0},
        }
        articles = [
            make_record(dek="A licensed description."),
            make_record(id="punch:2", source_id="punch",
                        headline="Osun tribunal ruling expected",
                        dek="An unlicensed description."),
        ]
        prompt = gist.build_topic_prompt("osun state elections", articles, sources)
        assert prompt.startswith("Recent coverage of: osun state elections")
        assert "A licensed description." in prompt
        assert "Osun tribunal ruling expected" in prompt
        assert "unlicensed" not in prompt


class TestStreamEvents:
    _sources = {"premium-times": {"id": "premium-times", "attribution_name": "PT",
                                  "rights_dek": 1, "rights_snippet": 1}}

    def _rows(self, n):
        return [make_record(id=f"premium-times:{i}", source_article_id=str(i),
                            headline=f"Osun update {i}") for i in range(n)]

    def test_streams_meta_deltas_done_and_caches(self, monkeypatch):
        _gist_cache.clear()
        monkeypatch.setattr(gist, "stream_writer",
                            lambda: (lambda system, prompt: iter(["The gist ", "so far."]), "groq:test"))
        events = _events("osun", self._rows(3), self._sources)
        assert [e["type"] for e in events] == ["meta", "delta", "delta", "done"]
        assert events[0]["articles"] == 3 and events[-1]["cached"] is False

        events = _events("osun", self._rows(3), self._sources)  # served from memory
        assert [e["type"] for e in events] == ["meta", "delta", "done"]
        assert events[1]["text"] == "The gist so far." and events[-1]["cached"] is True

    def test_unavailable_writer_becomes_a_status_line(self, monkeypatch):
        _gist_cache.clear()
        monkeypatch.setattr(gist, "stream_writer",
                            lambda: {"status": "offline", "detail": "The local model is offline."})
        events = _events("osun", self._rows(3), self._sources)
        assert [e["type"] for e in events] == ["meta", "status"]
        assert events[1]["status"] == "offline"

    def test_too_little_coverage_writes_nothing(self):
        _gist_cache.clear()
        events = _events("osun", self._rows(1), self._sources)
        assert [e["type"] for e in events] == ["meta", "status"]
        assert events[1]["status"] == "too little"

    def test_mid_stream_failure_is_reported(self, monkeypatch):
        _gist_cache.clear()

        def broken(system, prompt):
            yield "Half a "
            raise RuntimeError("connection dropped")

        monkeypatch.setattr(gist, "stream_writer", lambda: (broken, "groq:test"))
        events = _events("osun", self._rows(3), self._sources)
        assert [e["type"] for e in events] == ["meta", "delta", "status"]
        assert events[-1]["status"] == "error"
        assert _gist_cache == {}  # a broken run is not cached
