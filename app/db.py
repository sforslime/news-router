from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psycopg
import yaml
from psycopg.rows import dict_row

from .config import (
    DATABASE_URL_DIRECT,
    DATABASE_URL_READONLY,
    SOURCES_FILE,
)
from .normalize import now_iso

SCHEMA_FILE = Path(__file__).resolve().parent / "schema.sql"

# Fields compared between revisions to describe what a publisher changed.
_TRACKED = ("headline", "dek", "snippet", "byline", "image", "section")


def connect(url: str | None = None, *, readonly: bool = False) -> psycopg.Connection:
    """Open a connection.

    `readonly=True` is the serving path: many short-lived callers, so it goes
    through the pooled endpoint and is refused write permission at the server.

    Everything else writes — the daily ingest, schema setup, the migration —
    and goes over the direct endpoint instead. Writes happen once a day, so
    they gain nothing from pooling.

    Read-only is a matter of credentials, not of session state, and that
    distinction was learned the hard way. Running
    `SET default_transaction_read_only = on` leaves the setting on the server
    connection after the pooler takes it back, so the next caller inherits it —
    which is how the first version of this broke the nightly ingest. Passing it
    as a startup option instead is refused outright by Neon's pooler. What does
    work is connecting as a role that only holds SELECT: nothing to leak,
    nothing to reset, and a write fails on permissions.

    Autocommit is on throughout: every write is an idempotent single-row
    upsert, and holding transactions open across a pooler is how a serverless
    app runs a database out of connections.
    """
    if url is None:
        url = DATABASE_URL_READONLY if readonly else DATABASE_URL_DIRECT
    return psycopg.connect(url, row_factory=dict_row, autocommit=True)


class ServingConnection:
    """The read-only connection the API holds for its whole life, reopened when
    it dies.

    A warm instance can sit idle long enough for Neon to drop the connection
    under it, and psycopg does not reconnect on its own — every request after
    that failed with "the connection is closed". Serving only reads, so a read
    that fails on a dead connection is safe to retry once on a fresh one.
    """

    def __init__(self) -> None:
        self._conn = connect(readonly=True)

    def execute(self, *args: Any, **kwargs: Any) -> psycopg.Cursor:
        try:
            return self._conn.execute(*args, **kwargs)
        except psycopg.OperationalError:
            self._conn.close()
            self._conn = connect(readonly=True)
            return self._conn.execute(*args, **kwargs)

    def close(self) -> None:
        self._conn.close()


def init_db(conn: psycopg.Connection) -> None:
    conn.execute(SCHEMA_FILE.read_text())


def sync_sources(conn: psycopg.Connection, sources_file: str | None = None) -> int:
    """Load sources.yaml into the sources table. The file is the source of truth,
    so every row is overwritten on every sync."""
    data = yaml.safe_load(Path(sources_file or SOURCES_FILE).read_text())
    rows = data.get("sources", [])
    for s in rows:
        conn.execute(
            """
            INSERT INTO sources (id, name, homepage, adapter, endpoint, enabled, embed,
                                 full_text, attribution_name, timezone, added_at)
            VALUES (%(id)s,%(name)s,%(homepage)s,%(adapter)s,%(endpoint)s,%(enabled)s,%(embed)s,
                    %(full_text)s,%(attribution_name)s,%(timezone)s,%(added_at)s)
            ON CONFLICT(id) DO UPDATE SET
              name=excluded.name, homepage=excluded.homepage,
              adapter=excluded.adapter, endpoint=excluded.endpoint, enabled=excluded.enabled,
              embed=excluded.embed, full_text=excluded.full_text,
              attribution_name=excluded.attribution_name, timezone=excluded.timezone
            """,
            {
                "id": s["id"],
                "name": s["name"],
                "homepage": s["homepage"],
                "adapter": s["adapter"],
                "endpoint": s["endpoint"],
                "enabled": int(bool(s.get("enabled", False))),
                "embed": int(bool(s.get("embed", True))),
                "full_text": int(bool(s.get("full_text", True))),
                "attribution_name": s.get("attribution_name") or s["name"],
                "timezone": s.get("timezone", "Africa/Lagos"),
                "added_at": now_iso(),
            },
        )
    return len(rows)


def enabled_sources(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT * FROM sources WHERE enabled = 1 ORDER BY id"
    ).fetchall()


def upsert_article(conn: psycopg.Connection, rec: dict[str, Any]) -> str:
    """Insert, update-with-revision, or skip. Returns 'new' | 'updated' | 'unchanged'."""
    existing = conn.execute(
        "SELECT * FROM articles WHERE id = %s", (rec["id"],)
    ).fetchone()

    cols = (
        "id, source_id, source_article_id, headline, dek, byline, published_at, "
        "published_at_reported, updated_at, first_seen_at, canonical_url, section, "
        "snippet, image, language, wire_source, paywalled, sponsored, content_hash, entities, "
        "lead_text"
    )
    names = [c.strip() for c in cols.split(",")]
    placeholders = ", ".join(f"%({c})s" for c in names)
    payload = {c: rec.get(c) for c in names}

    if existing is None:
        conn.execute(f"INSERT INTO articles ({cols}) VALUES ({placeholders})", payload)
        conn.execute(
            """INSERT INTO article_revisions (article_id, revision, content_hash, headline, dek, snippet, seen_at, changed)
               VALUES (%s,1,%s,%s,%s,%s,%s,'[]')""",
            (rec["id"], rec["content_hash"], rec["headline"], rec.get("dek"),
             rec.get("snippet"), rec["first_seen_at"]),
        )
        return "new"

    if existing["content_hash"] == rec["content_hash"]:
        if rec.get("lead_text") and not existing["lead_text"]:
            # Read before lead text was kept, or after it was purged: fill it in.
            conn.execute("UPDATE articles SET lead_text = %s WHERE id = %s",
                         (rec["lead_text"], rec["id"]))
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
        """UPDATE articles SET headline=%(headline)s, dek=%(dek)s, byline=%(byline)s,
             published_at=%(published_at)s, published_at_reported=%(published_at_reported)s,
             updated_at=%(updated_at)s, canonical_url=%(canonical_url)s, section=%(section)s,
             snippet=%(snippet)s, image=%(image)s, language=%(language)s, wire_source=%(wire_source)s,
             paywalled=%(paywalled)s, sponsored=%(sponsored)s, content_hash=%(content_hash)s,
             entities=%(entities)s, revision=%(revision)s,
             lead_text=COALESCE(%(lead_text)s, lead_text)
           WHERE id=%(id)s""",
        {**payload, "revision": revision},
    )
    conn.execute(
        """INSERT INTO article_revisions (article_id, revision, content_hash, headline, dek, snippet, seen_at, changed)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
        (rec["id"], revision, rec["content_hash"], rec["headline"], rec.get("dek"),
         rec.get("snippet"), now_iso(), json.dumps(changed)),
    )
    return "updated"


def purge_leads(conn: psycopg.Connection, hours: int = 72) -> int:
    """Clear opening paragraphs once a report is older than gists look back.
    They exist only to write gists, so they are not kept beyond that."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat().replace("+00:00", "Z")
    cur = conn.execute(
        "UPDATE articles SET lead_text = NULL WHERE lead_text IS NOT NULL AND first_seen_at < %s",
        (cutoff,),
    )
    return cur.rowcount


def mark_ingest(conn: psycopg.Connection, source_id: str, error: str | None = None) -> None:
    conn.execute(
        "UPDATE sources SET last_ingest_at = %s, last_error = %s WHERE id = %s",
        (now_iso(), error, source_id),
    )


def set_state(conn: psycopg.Connection, key: str, value: dict[str, Any]) -> None:
    conn.execute(
        """INSERT INTO pipeline_state (key, value, updated_at) VALUES (%s, %s, %s)
           ON CONFLICT (key) DO UPDATE SET
             value = excluded.value, updated_at = excluded.updated_at""",
        (key, json.dumps(value, ensure_ascii=False), now_iso()),
    )


def get_state(conn: psycopg.Connection, key: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT value, updated_at FROM pipeline_state WHERE key = %s", (key,)
    ).fetchone()
    if row is None:
        return None
    value = json.loads(row["value"])
    value["at"] = row["updated_at"]
    return value


def counts(conn: psycopg.Connection) -> dict[str, int]:
    row = conn.execute(
        """SELECT (SELECT COUNT(*) FROM sources)                    AS sources,
                  (SELECT COUNT(*) FROM sources WHERE enabled = 1)  AS sources_enabled,
                  (SELECT COUNT(*) FROM articles)                   AS articles,
                  (SELECT COUNT(*) FROM article_revisions)          AS revisions,
                  (SELECT COUNT(*) FROM clusters)                   AS clusters"""
    ).fetchone()
    return {k: int(v) for k, v in row.items()}
