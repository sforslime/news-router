"""Digest CLI:  python -m app.digest [--max-gists N]

Clusters the recent window, then writes gists. The deployed equivalent is
/v1/admin/digest, run on a schedule after ingest.
"""
from __future__ import annotations

import argparse
import json

from . import cluster, db, gist


def main() -> int:
    parser = argparse.ArgumentParser(description="Cluster recent articles and write story gists")
    parser.add_argument("--max-gists", type=int, default=25, help="max model calls this run")
    args = parser.parse_args()

    conn = db.connect()
    db.init_db(conn)

    cluster_stats = cluster.run(conn)
    print(f"clustering: {json.dumps(cluster_stats)}")

    gist_stats = gist.generate(conn, max_gists=args.max_gists)
    print(f"gists:      {json.dumps(gist_stats)}")

    return 1 if gist_stats.get("status") == "degraded" else 0


if __name__ == "__main__":
    raise SystemExit(main())
