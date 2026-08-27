"""Copy the local SQLite database into Postgres:  python -m app.migrate_from_sqlite

One-time move. Reads router.db as it stands and writes every row into the
Postgres database named by DATABASE_URL, preserving ids, first_seen_at and the
full revision history — none of which can be recovered by re-ingesting, because
publishers only serve what is current.

Re-running is safe: rows already present are left alone.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from . import db

# The generated tsvector is computed by Postgres, so it is never copied.
TABLES = {
    "sources": None,
    "articles": None,
    "article_revisions": None,
    "clusters": None,
    "api_keys": None,
}


def _columns(pg, table: str) -> list[str]:
    rows = pg.execute(
        """SELECT column_name FROM information_schema.columns
           WHERE table_name = %s AND is_generated = 'NEVER'
           ORDER BY ordinal_position""",
        (table,),
    ).fetchall()
    return [r["column_name"] for r in rows]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sqlite", default="router.db", help="path to the SQLite file")
    args = ap.parse_args()

    path = Path(args.sqlite)
    if not path.exists():
        print(f"no such file: {path}", file=sys.stderr)
        return 1

    lite = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    lite.row_factory = sqlite3.Row

    with db.connect() as pg:
        db.init_db(pg)
        for table in TABLES:
            cols = _columns(pg, table)
            # Only carry columns both sides know about, so a schema that has
            # moved on since the SQLite file was written still migrates.
            have = {r[1] for r in lite.execute(f"PRAGMA table_info({table})")}
            cols = [c for c in cols if c in have]
            if not cols:
                print(f"  {table}: no shared columns, skipped")
                continue

            rows = lite.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()
            if not rows:
                print(f"  {table}: empty")
                continue

            placeholders = ", ".join(["%s"] * len(cols))
            sql = (
                f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
                f"ON CONFLICT DO NOTHING"
            )
            with pg.cursor() as cur:
                cur.executemany(sql, [tuple(r[c] for c in cols) for r in rows])
            print(f"  {table}: {len(rows)} rows")

        print()
        print(", ".join(f"{k}={v}" for k, v in db.counts(pg).items()))

    lite.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
