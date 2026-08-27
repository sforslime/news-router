"""API key admin:  python -m app.keys issue "Acme Corp" --plan pro --rate 600
                   python -m app.keys list
                   python -m app.keys revoke <key_hash_prefix>
"""
from __future__ import annotations

import argparse

from . import db
from .auth import mint_key


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage router API keys")
    sub = parser.add_subparsers(dest="cmd", required=True)

    issue = sub.add_parser("issue", help="mint a new key")
    issue.add_argument("name")
    issue.add_argument("--plan", default="free")
    issue.add_argument("--rate", type=int, default=120, help="requests per minute")

    sub.add_parser("list", help="list issued keys")

    revoke = sub.add_parser("revoke", help="disable a key by hash prefix")
    revoke.add_argument("prefix")

    args = parser.parse_args()
    conn = db.connect()
    db.init_db(conn)

    if args.cmd == "issue":
        raw = mint_key(conn, args.name, args.plan, args.rate)
        print(f"key issued for {args.name!r} ({args.plan}, {args.rate}/min)")
        print(f"\n  {raw}\n")
        print("Store it now — only the hash is kept, so it cannot be shown again.")
    elif args.cmd == "list":
        rows = conn.execute("SELECT * FROM api_keys ORDER BY created_at DESC").fetchall()
        if not rows:
            print("No keys issued.")
        for r in rows:
            state = "enabled" if r["enabled"] else "revoked"
            print(f"  {r['key_hash'][:16]}  {r['name']:<24} {r['plan']:<8} {r['rate_per_min']:>5}/min  {state}")
    elif args.cmd == "revoke":
        cur = conn.execute(
            "UPDATE api_keys SET enabled = 0 WHERE key_hash LIKE %s", (args.prefix + "%",)
        )
        print(f"revoked {cur.rowcount} key(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
