"""One-off database preparation:  python -m app.setup

Creates the schema if it is missing and loads app/sources.yaml into the sources
table. Safe to re-run — every statement is idempotent. Run it once against a new
database, and again after changing sources.yaml.
"""
from __future__ import annotations

import sys

from . import db


def main() -> int:
    with db.connect() as conn:
        db.init_db(conn)
        n = db.sync_sources(conn)
        c = db.counts(conn)
    print(f"schema ready, {n} sources synced")
    print(", ".join(f"{k}={v}" for k, v in c.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
