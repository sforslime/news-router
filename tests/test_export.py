import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db, fulltext
from app.normalize import body_text
from conftest import make_record


class TestBodyText:
    def test_keeps_paragraphs_and_subheadings(self):
        html = ("<p>First <strong>point</strong>.</p><h4><strong>A subhead</strong></h4>"
                "<ul><li>one</li><li>two</li></ul><p>Last.</p>")
        assert body_text(html) == "First point.\n\n### A subhead\n\n- one\n\n- two\n\nLast."

    def test_drops_ads_scripts_figures_and_boilerplate(self):
        html = ('<div class="ad"><a>Add us on Google</a></div><p>Report.</p>'
                "<script>track()</script><figure><img/><figcaption>Photo</figcaption></figure>"
                "<p>READ ALSO: Something else</p><p>Join our WhatsApp channel</p><p>End.</p>")
        assert body_text(html) == "Report.\n\nEnd."

    def test_bare_text_without_blocks_survives(self):
        assert body_text("Just text<br>and more") == "Just text and more"

    def test_nothing_in_nothing_out(self):
        assert body_text(None) == ""


class _Resp:
    def __init__(self, posts, ctype="application/json"):
        self.status_code = 200
        self.headers = {"content-type": ctype}
        self._posts = posts

    def json(self):
        return self._posts


class TestFetchTexts:
    SOURCES = {
        "punch": {"id": "punch", "adapter": "wordpress", "endpoint": "https://p/wp-json/wp/v2", "full_text": 1},
        "icir": {"id": "icir", "adapter": "wordpress", "endpoint": "https://i/wp-json/wp/v2", "full_text": 1},
        "vanguard": {"id": "vanguard", "adapter": "rss", "endpoint": "https://v/feed", "full_text": 1},
        "off": {"id": "off", "adapter": "wordpress", "endpoint": "https://o/wp-json/wp/v2", "full_text": 0},
    }

    def _row(self, source, n):
        return {"id": f"{source}:{n}", "source_id": source, "source_article_id": str(n)}

    def _fake(self, monkeypatch, refuse_big_batches=()):
        calls = []

        def get(self_client, url, params):
            ids = params["include"].split(",")
            calls.append((url, ids))
            if any(h in url for h in refuse_big_batches) and len(ids) > fulltext.SMALL_BATCH:
                return _Resp("<html>", "text/html")
            return _Resp([{"id": int(i), "content": {"rendered": f"<p>Body {i}.</p>"}} for i in ids])

        monkeypatch.setattr(fulltext.httpx.Client, "get", get)
        fulltext._cache.clear()
        return calls

    def test_one_request_per_newsroom_and_none_where_it_cannot_ask(self, monkeypatch):
        calls = self._fake(monkeypatch)
        rows = [self._row("punch", 1), self._row("punch", 2), self._row("vanguard", 3), self._row("off", 4)]
        out = fulltext.fetch_texts(rows, self.SOURCES)
        assert out == {"punch:1": "Body 1.", "punch:2": "Body 2.", "vanguard:3": None, "off:4": None}
        assert calls == [("https://p/wp-json/wp/v2/posts", ["1", "2"])]

    def test_refused_batch_is_asked_again_a_few_at_a_time(self, monkeypatch):
        calls = self._fake(monkeypatch, refuse_big_batches=("https://i/",))
        rows = [self._row("icir", n) for n in range(1, 11)]
        out = fulltext.fetch_texts(rows, self.SOURCES)
        assert all(out[f"icir:{n}"] == f"Body {n}." for n in range(1, 11))
        assert [len(ids) for _, ids in calls] == [10, 4, 4, 2]

    def test_repeat_downloads_come_from_memory(self, monkeypatch):
        calls = self._fake(monkeypatch)
        rows = [self._row("punch", 1)]
        fulltext.fetch_texts(rows, self.SOURCES)
        fulltext.fetch_texts(rows, self.SOURCES)
        assert len(calls) == 1


class TestExportRoute:
    def _client(self, conn, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.routes import export as export_route

        # Punch has full text, Vanguard (RSS) does not.
        monkeypatch.setattr(export_route, "fetch_texts", lambda rows, srcs: {
            r["id"]: (f"Full text of {r['id']}." if r["source_id"] != "vanguard" else None) for r in rows})
        app = FastAPI()
        app.include_router(export_route.router)
        app.state.conn = conn
        return TestClient(app)

    def _seed(self, conn, n):
        for i in range(1, n + 1):
            source = "vanguard" if i == 1 else "punch"
            db.upsert_article(conn, make_record(
                id=f"{source}:{i}", source_id=source, source_article_id=str(i),
                headline=f"Tinubu story {i}", published_at=f"2026-10-01T{i % 24:02d}:{i // 24:02d}:00Z",
                canonical_url=f"https://example.com/{i}"))

    def test_markdown_carries_full_text_or_the_link(self, conn, monkeypatch):
        self._seed(conn, 3)
        r = self._client(conn, monkeypatch).get("/v1/export", params={"q": "tinubu", "since": "2026-10-01"})
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
        assert r.headers["X-Total"] == "3" and r.headers["X-Full-Text"] == "2"
        assert "X-Next-Cursor" not in r.headers
        md = r.text
        assert md.startswith("# “tinubu”: Nigerian News Router")
        assert "3 reports published since 1 Oct 2026" in md
        assert "## Tinubu story 3" in md and "Full text of punch:3." in md
        assert "[Original](https://example.com/3)" in md
        assert "Full text not available here. Read it at Vanguard: https://example.com/1" in md

    def test_pages_join_into_every_report_once(self, conn, monkeypatch):
        from app.routes import export as export_route

        self._seed(conn, export_route.PAGE + 5)
        client = self._client(conn, monkeypatch)
        r1 = client.get("/v1/export", params={"q": "tinubu"})
        r2 = client.get("/v1/export", params={"q": "tinubu", "cursor": r1.headers["X-Next-Cursor"]})
        assert r1.headers["X-Count"] == str(export_route.PAGE) and r2.headers["X-Count"] == "5"
        assert "X-Next-Cursor" not in r2.headers
        assert "Nigerian News Router" not in r2.text   # the title is on the first page only
        joined = r1.text + r2.text
        heads = [line for line in joined.splitlines() if line.startswith("## ")]
        assert len(heads) == len(set(heads)) == export_route.PAGE + 5
