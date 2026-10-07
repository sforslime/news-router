"""Download every report on a topic, full text included, into one .md file.

    python -m app.export tinubu --since 2026-10-01
    python -m app.export "fuel subsidy" --since 2026-09-01 --until 2026-09-30 --out subsidy.md
    python -m app.export tinubu --source punch,premium-times

Asks the live router (/v1/export) page by page and joins the pages; the router
decides the layout. Dates are days in Lagos time, or full ISO timestamps.
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timedelta, timezone

import httpx

from .config import USER_AGENT

LAGOS = timezone(timedelta(hours=1))


def _bound(value: str | None, end: bool) -> str | None:
    """'2026-10-01' -> the start (or end) of that day in Lagos, as UTC ISO."""
    if not value:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        day = datetime.fromisoformat(value).replace(tzinfo=LAGOS)
        if end:
            day += timedelta(days=1, seconds=-1)
        return day.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return value


def _filename(q: str, since: str | None, until: str | None) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", q.lower()).strip("-") or "export"
    span = "_".join(x for x in (since, until) if x)
    return f"{slug}_{span}.md" if span else f"{slug}.md"


def main() -> int:
    parser = argparse.ArgumentParser(description="Export every report on a topic, with full text, to Markdown")
    parser.add_argument("q", help="the topic, as you would search it")
    parser.add_argument("--since", help="first day, e.g. 2026-10-01")
    parser.add_argument("--until", help="last day, e.g. 2026-10-31")
    parser.add_argument("--source", help="newsroom ids, comma separated")
    parser.add_argument("--out", help="file to write; named after the topic and dates by default")
    parser.add_argument("--base", default="https://news-router.vercel.app", help="router address")
    args = parser.parse_args()

    params = {"q": args.q}
    for key, value in (("since", _bound(args.since, False)), ("until", _bound(args.until, True)),
                       ("source", args.source)):
        if value:
            params[key] = value

    out = args.out or _filename(args.q, args.since, args.until)
    url = args.base.rstrip("/") + "/v1/export"
    done = full = 0
    with httpx.Client(timeout=120, headers={"User-Agent": USER_AGENT}) as client, \
            open(out, "w", encoding="utf-8") as f:
        while True:
            resp = client.get(url, params=params)
            if resp.status_code != 200:
                print(f"\nThe router answered {resp.status_code}: {resp.text[:200]}", file=sys.stderr)
                return 1
            f.write(resp.text)
            total = int(resp.headers.get("X-Total", 0))
            done += int(resp.headers.get("X-Count", 0))
            full += int(resp.headers.get("X-Full-Text", 0))
            print(f"\r{done} of {total} reports", end="", flush=True)
            cursor = resp.headers.get("X-Next-Cursor")
            if not cursor:
                break
            params["cursor"] = cursor

    print(f"\nWrote {out}: {done} reports, {full} with full text.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
