"""Fold Punch's old RSS rows into its WordPress rows:  python -m app.remap_punch

One-time cleanup after Punch moved from RSS to wp-json. Its feed guids are
permalinks, so RSS rows were keyed on a hash of the URL ('punch:3f9a…') while
wp-json keys on the post id ('punch:905512'). The same report can therefore
exist twice. Each old row is matched to its new one by canonical URL; the new
row keeps the earlier first_seen_at and the old row's cluster, and the old row
and its revisions are removed. Old rows with no wp-json twin are left alone.

Run it after the first wp-json ingest. Re-running is safe.
"""
from __future__ import annotations

import argparse
import sys

from . import db


def remap(conn, source_id: str) -> dict[str, int]:
    pairs = conn.execute(
        """SELECT o.id AS old_id, n.id AS new_id
           FROM articles o
           JOIN articles n ON n.source_id = o.source_id
                          AND n.canonical_url = o.canonical_url
                          AND n.id <> o.id
           WHERE o.source_id = %s
             AND o.source_article_id !~ '^[0-9]+$'
             AND n.source_article_id ~ '^[0-9]+$'""",
        (source_id,),
    ).fetchall()

    for p in pairs:
        conn.execute(
            """UPDATE articles n SET
                 first_seen_at = LEAST(n.first_seen_at, o.first_seen_at),
                 cluster_id    = COALESCE(o.cluster_id, n.cluster_id)
               FROM articles o
               WHERE n.id = %s AND o.id = %s""",
            (p["new_id"], p["old_id"]),
        )
        conn.execute(
            "UPDATE clusters SET lead_article_id = %s WHERE lead_article_id = %s",
            (p["new_id"], p["old_id"]),
        )
        conn.execute("DELETE FROM article_revisions WHERE article_id = %s", (p["old_id"],))
        conn.execute("DELETE FROM articles WHERE id = %s", (p["old_id"],))

    left = conn.execute(
        """SELECT count(*) AS n FROM articles
           WHERE source_id = %s AND source_article_id !~ '^[0-9]+$'""",
        (source_id,),
    ).fetchone()["n"]
    return {"merged": len(pairs), "old_rows_left": left}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", default="punch")
    args = ap.parse_args()
    with db.connect() as conn:
        stats = remap(conn, args.source)
    print(f"{args.source}: merged {stats['merged']}, "
          f"{stats['old_rows_left']} old rows with no wp-json twin left as they are")
    return 0


if __name__ == "__main__":
    sys.exit(main())
