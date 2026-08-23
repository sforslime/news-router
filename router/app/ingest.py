"""Ingestion CLI:  python -m app.ingest [--source ID] [--limit N] [--since ISO]"""
from __future__ import annotations

import argparse
import sys

from . import db
from .adapters import get_adapter
from .adapters.base import FetchError


def ingest_source(conn, source: dict, limit: int, since: str | None) -> dict:
    adapter = get_adapter(source["adapter"])
    stats = {"source": source["id"], "new": 0, "updated": 0, "unchanged": 0, "error": None}
    try:
        records = adapter.fetch(source, limit=limit, since=since)
    except (FetchError, KeyError) as exc:
        stats["error"] = str(exc)
        db.mark_ingest(conn, source["id"], error=str(exc))
        return stats

    for rec in records:
        stats[db.upsert_article(conn, rec)] += 1
    db.mark_ingest(conn, source["id"], error=None)
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest news sources into the router")
    parser.add_argument("--source", help="source id; default is every enabled source")
    parser.add_argument("--limit", type=int, default=50, help="max articles per source")
    parser.add_argument("--since", help="only fetch items modified after this ISO timestamp")
    args = parser.parse_args()

    conn = db.connect()
    db.init_db(conn)
    db.sync_sources(conn)

    sources = db.enabled_sources(conn)
    if args.source:
        sources = [s for s in sources if s["id"] == args.source]
        if not sources:
            print(f"error: {args.source!r} is not an enabled source. "
                  f"Set enabled: true in app/sources.yaml first.", file=sys.stderr)
            return 1

    if not sources:
        print("No enabled sources. Nothing ingested.", file=sys.stderr)
        return 1

    failed = False
    for source in sources:
        stats = ingest_source(conn, dict(source), args.limit, args.since)
        if stats["error"]:
            failed = True
            print(f"  {stats['source']:<16} FAILED  {stats['error']}")
        else:
            print(f"  {stats['source']:<16} new={stats['new']:<4} updated={stats['updated']:<4} unchanged={stats['unchanged']}")

    print("\n" + ", ".join(f"{k}={v}" for k, v in db.counts(conn).items()))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
