import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db, usage
from conftest import make_record


@pytest.fixture
def client(conn, monkeypatch):
    """The real app, its middleware included, on the scratch schema. Usage rows
    are written through the fixture connection."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app()
    app.state.conn = conn
    monkeypatch.setattr(usage, "_conn", conn)
    db.upsert_article(conn, make_record())
    # Not entered as a context manager: that would run the lifespan, which
    # opens the production read-only connection.
    return TestClient(app)


def events(conn):
    return conn.execute("SELECT * FROM usage_events ORDER BY id").fetchall()


class TestRecording:
    def test_a_search_is_recorded_with_its_terms_and_result_count(self, client, conn):
        r = client.get("/v1/search", params={"q": "minister", "since": "2026-08-01"})
        assert r.status_code == 200
        [e] = events(conn)
        assert e["kind"] == "search" and e["path"] == "/v1/search"
        assert json.loads(e["params"]) == {"q": "minister", "since": "2026-08-01"}
        assert e["results"] == 1 and e["status"] == 200
        assert e["visitor"] and len(e["visitor"]) == 12

    def test_page_visit_is_recorded_but_its_furniture_is_not(self, client, conn):
        client.get("/")
        client.get("/v1/health", headers={"referer": "http://testserver/"})
        client.get("/v1/sources", headers={"referer": "http://testserver/"})
        assert [e["kind"] for e in events(conn)] == ["visit"]

    def test_admin_routes_are_not_recorded(self, client, conn):
        client.get("/v1/admin/usage")
        assert events(conn) == []

    def test_a_dated_feed_search_records_the_total(self, client, conn):
        client.get("/v1/articles", params={"q": "minister"})
        [e] = events(conn)
        assert e["kind"] == "feed" and e["results"] == 1

    def test_a_failed_write_never_fails_the_request(self, client, conn, monkeypatch):
        class Broken:
            def execute(self, *a, **k):
                raise RuntimeError("database is down")

        monkeypatch.setattr(usage, "_conn", Broken())
        assert client.get("/v1/search", params={"q": "minister"}).status_code == 200


class TestReport:
    def test_needs_the_admin_token(self, client, monkeypatch):
        from app.routes import admin

        monkeypatch.setattr(admin, "ADMIN_TOKEN", "")
        assert client.get("/v1/admin/usage").status_code == 503
        monkeypatch.setattr(admin, "ADMIN_TOKEN", "s3cret")
        assert client.get("/v1/admin/usage").status_code == 401
        assert client.get("/v1/admin/usage", headers={"Authorization": "Bearer nope"}).status_code == 401

    def test_counts_searches_and_downloads(self, client, conn, monkeypatch):
        from app.routes import admin

        monkeypatch.setattr(admin, "ADMIN_TOKEN", "s3cret")
        client.get("/")
        client.get("/v1/search", params={"q": "Minister"})
        client.get("/v1/search", params={"q": "minister"})
        # A download's first page and a follow-on page: one download.
        for params in ({"q": "tinubu"}, {"q": "tinubu", "cursor": "x"}):
            conn.execute(
                "INSERT INTO usage_events (at, kind, path, params, visitor, results) VALUES (%s,'export','/v1/export',%s,'v1',50)",
                (usage.now_iso(), json.dumps(params)))
        d = client.get("/v1/admin/usage", headers={"Authorization": "Bearer s3cret"}).json()
        assert d["totals"]["visits"] == 1
        assert d["totals"]["searches"] == 2
        assert d["totals"]["downloads"] == 1 and d["totals"]["reports_downloaded"] == 100
        assert d["top_searches"][0]["q"] == "minister" and d["top_searches"][0]["times"] == 2
        assert d["downloads"][0]["q"] == "tinubu" and d["downloads"][0]["times"] == 1
