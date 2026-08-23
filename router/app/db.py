from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

import yaml

from .config import DB_PATH, SOURCES_FILE, SOURCES_LOCAL_FILE
from .normalize import now_iso

SCHEMA_FILE = Path(__file__).resolve().parent / "schema.sql"

# Fields compared between revisions to describe what a publisher changed.
_TRACKED = ("headline", "dek", "snippet", "byline", "image", "section")


def connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_FILE.read_text())
    conn.commit()


def sync_sources(conn: sqlite3.Connection, sources_file: str | None = None) -> int:
    """Load sources.yaml into the sources table. The file is the source of truth
    for licensing flags, so they are overwritten on every sync."""
    data = yaml.safe_load(Path(sources_file or SOURCES_FILE).read_text())
    rows = data.get("sources", [])
    licences = _local_licences()
    for s in rows:
        conn.execute(
            """
            INSERT INTO sources (id, name, homepage, tier, adapter, endpoint, enabled,
                                 license_status, rights_dek, rights_snippet, rights_image,
                                 attribution_name, timezone, added_at)
            VALUES (:id,:name,:homepage,:tier,:adapter,:endpoint,:enabled,
                    :license_status,:rights_dek,:rights_snippet,:rights_image,
                    :attribution_name,:timezone,:added_at)
            ON CONFLICT(id) DO UPDATE SET
              name=excluded.name, homepage=excluded.homepage, tier=excluded.tier,
              adapter=excluded.adapter, endpoint=excluded.endpoint, enabled=excluded.enabled,
              license_status=excluded.license_status, rights_dek=excluded.rights_dek,
              rights_snippet=excluded.rights_snippet, rights_image=excluded.rights_image,
              attribution_name=excluded.attribution_name, timezone=excluded.timezone
            """,
            {
                "id": s["id"],
                "name": s["name"],
                "homepage": s["homepage"],
                "tier": int(s["tier"]),
                "adapter": s["adapter"],
                "endpoint": s["endpoint"],
                "enabled": int(bool(s.get("enabled", False))),
                "license_status": licences.get(
                    s["id"], s.get("license_status", "none")
                ),
                "rights_dek": int(bool(s.get("rights_dek", False))),
                "rights_snippet": int(bool(s.get("rights_snippet", False))),
                "rights_image": int(bool(s.get("rights_image", False))),
                "attribution_name": s.get("attribution_name") or s["name"],
                "timezone": s.get("timezone", "Africa/Lagos"),
                "added_at": now_iso(),
            },
        )
    conn.commit()
    return len(rows)


def _local_licences() -> dict[str, str]:
    """Agreement status per source id, from the git-ignored local overlay.

    sources.yaml is public and deliberately says nothing about where a
    conversation stands. Absent the overlay every source reads as 'none',
    which is the safe default: it grants nothing it should not.
    """
    path = Path(SOURCES_LOCAL_FILE)
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    return {str(k): str(v) for k, v in (data.get("license_status") or {}).items()}


def enabled_sources(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM sources WHERE enabled = 1 ORDER BY tier, id").fetchall()


def _index_fts(conn: sqlite3.Connection, rec: dict[str, Any]) -> None:
    conn.execute("DELETE FROM articles_fts WHERE article_id = ?", (rec["id"],))
    entities = " ".join(json.loads(rec.get("entities") or "[]"))
    conn.execute(
        "INSERT INTO articles_fts (article_id, headline, dek, snippet, entities) VALUES (?,?,?,?,?)",
        (rec["id"], rec.get("headline") or "", rec.get("dek") or "", rec.get("snippet") or "", entities),
    )


def upsert_article(conn: sqlite3.Connection, rec: dict[str, Any]) -> str:
    """Insert, update-with-revision, or skip. Returns 'new' | 'updated' | 'unchanged'."""
    existing = conn.execute(
        "SELECT * FROM articles WHERE id = ?", (rec["id"],)
    ).fetchone()

    cols = (
        "id, source_id, source_article_id, headline, dek, byline, published_at, "
        "published_at_reported, updated_at, first_seen_at, canonical_url, section, "
        "snippet, image, language, wire_source, paywalled, sponsored, content_hash, entities"
    )
    placeholders = ", ".join(f":{c.strip()}" for c in cols.split(","))
    payload = {c.strip(): rec.get(c.strip()) for c in cols.split(",")}

    if existing is None:
        conn.execute(f"INSERT INTO articles ({cols}) VALUES ({placeholders})", payload)
        conn.execute(
            """INSERT INTO article_revisions (article_id, revision, content_hash, headline, dek, snippet, seen_at, changed)
               VALUES (?,1,?,?,?,?,?,'[]')""",
            (rec["id"], rec["content_hash"], rec["headline"], rec.get("dek"), rec.get("snippet"), rec["first_seen_at"]),
        )
        _index_fts(conn, rec)
        return "new"

    if existing["content_hash"] == rec["content_hash"]:
        return "unchanged"

    changed = [f for f in _TRACKED if (existing[f] or None) != (rec.get(f) or None)]
    # The hash covers the body, which is not stored and so cannot be diffed.
    # A moved hash with every stored field intact means the body itself was
    # rewritten; say so rather than logging a revision that changed nothing.
    if not changed:
        changed = ["body"]
    revision = int(existing["revision"]) + 1

    # first_seen_at is never overwritten — it is the one timestamp a publisher
    # cannot revise out from under us.
    payload["first_seen_at"] = existing["first_seen_at"]
    conn.execute(
        """UPDATE articles SET headline=:headline, dek=:dek, byline=:byline,
             published_at=:published_at, published_at_reported=:published_at_reported,
             updated_at=:updated_at, canonical_url=:canonical_url, section=:section,
             snippet=:snippet, image=:image, language=:language, wire_source=:wire_source,
             paywalled=:paywalled, sponsored=:sponsored, content_hash=:content_hash, entities=:entities,
             revision=:revision
           WHERE id=:id""",
        {**payload, "revision": revision},
    )
    conn.execute(
        """INSERT INTO article_revisions (article_id, revision, content_hash, headline, dek, snippet, seen_at, changed)
           VALUES (?,?,?,?,?,?,?,?)""",
        (rec["id"], revision, rec["content_hash"], rec["headline"], rec.get("dek"),
         rec.get("snippet"), now_iso(), json.dumps(changed)),
    )
    _index_fts(conn, rec)
    return "updated"


def mark_ingest(conn: sqlite3.Connection, source_id: str, error: str | None = None) -> None:
    conn.execute(
        "UPDATE sources SET last_ingest_at = ?, last_error = ? WHERE id = ?",
        (now_iso(), error, source_id),
    )
    conn.commit()


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        "sources": conn.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"],
        "sources_enabled": conn.execute("SELECT COUNT(*) c FROM sources WHERE enabled=1").fetchone()["c"],
        "articles": conn.execute("SELECT COUNT(*) c FROM articles").fetchone()["c"],
        "revisions": conn.execute("SELECT COUNT(*) c FROM article_revisions").fetchone()["c"],
        "clusters": conn.execute("SELECT COUNT(*) c FROM clusters").fetchone()["c"],
    }
