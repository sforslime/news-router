"""Ingestion CLI:  python -m app.ingest [--source ID] [--limit N] [--since ISO]"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import db
from .adapters import get_adapter
from .adapters.base import FetchError

FETCH_WORKERS = 8


def fetch_source(source: dict, limit: int, since: str | None) -> tuple[list, str | None]:
    """Read one newsroom over the network. Touches no database, so several can
    run at once."""
    try:
        return get_adapter(source["adapter"]).fetch(source, limit=limit, since=since), None
    except (FetchError, KeyError) as exc:
        return [], str(exc)


def store_source(conn, source: dict, records: list, error: str | None) -> dict:
    """Save one newsroom's fetch result. One connection, so this runs serially."""
    stats = {"source": source["id"], "new": 0, "updated": 0, "unchanged": 0, "error": error}
    if error:
        db.mark_ingest(conn, source["id"], error=error)
        return stats
    for rec in records:
        stats[db.upsert_article(conn, rec)] += 1
    db.mark_ingest(conn, source["id"], error=None)
    return stats


def ingest_source(conn, source: dict, limit: int, since: str | None) -> dict:
    return store_source(conn, source, *fetch_source(source, limit, since))


def ingest_all(conn, sources: list[dict], limit: int, since_for) -> list[dict]:
    """Every newsroom fetched side by side, saved as each one arrives.

    Fetching is nearly all waiting on slow newsroom servers, so running the
    fetches at once keeps a two-dozen-outlet morning read well inside the
    function time limit, where one after another it was not.
    """
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        futures = {pool.submit(fetch_source, s, limit, since_for(s)): s for s in sources}
        return [store_source(conn, futures[f], *f.result()) for f in as_completed(futures)]


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
    results = ingest_all(conn, [dict(s) for s in sources], args.limit, lambda _: args.since)
    for stats in sorted(results, key=lambda r: r["source"]):
        if stats["error"]:
            failed = True
            print(f"  {stats['source']:<16} FAILED  {stats['error']}")
        else:
            print(f"  {stats['source']:<16} new={stats['new']:<4} updated={stats['updated']:<4} unchanged={stats['unchanged']}")

    print("\n" + ", ".join(f"{k}={v}" for k, v in db.counts(conn).items()))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
