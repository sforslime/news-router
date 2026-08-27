"""Digest CLI:  python -m app.digest [--max-gists N]

Clusters the recent window, then writes gists. The deployed equivalent is
/v1/admin/digest, run on a schedule after ingest.
"""
from __future__ import annotations

import argparse
import json

from . import cluster, db, gist


def _state_of(stats: dict) -> dict:
    """What the serving path needs to explain a missing gist — outcome, not
    the full error list."""
    return {
        "status": stats.get("status"),
        "backend": stats.get("backend"),
        "model": stats.get("model"),
        "detail": stats.get("detail"),
        "generated": stats.get("generated", 0),
        "errors": len(stats.get("errors", [])),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Cluster recent articles and write story gists")
    parser.add_argument("--max-gists", type=int, default=25, help="max model calls this run")
    args = parser.parse_args()

    conn = db.connect()
    db.init_db(conn)

    cluster_stats = cluster.run(conn)
    print(f"clustering: {json.dumps(cluster_stats)}")

    gist_stats = gist.generate(conn, max_gists=args.max_gists)
    db.set_state(conn, "gist_writer", _state_of(gist_stats))
    print(f"gists:      {json.dumps(gist_stats)}")

    return 1 if gist_stats.get("status") == "degraded" else 0


if __name__ == "__main__":
    raise SystemExit(main())
