"""Full article text, fetched live from the newsroom for the export.

Nothing here is stored. The router asks each newsroom's public WordPress
endpoint for the posts it needs, keeps the text in memory for half an hour so
a repeated download does not ask twice, and throws it away. Corrections and
deletions by the newsroom therefore show up at once.

Only WordPress newsrooms can be asked for a past article by its id. RSS feeds
hold the last few hours and nothing else, so their reports come back as None,
as do newsrooms switched off with `full_text: false` and failed requests.
"""
from __future__ import annotations

import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx

from .config import HTTP_TIMEOUT, USER_AGENT
from .normalize import body_text

BATCH = 100        # posts per request: WordPress's per_page ceiling
SMALL_BATCH = 4    # ICIR and Peoples Gazette serve a web page past four ids
CACHE_TTL = 30 * 60
FETCH_WORKERS = 8

_cache: dict[str, tuple[float, str]] = {}


def _get(client: httpx.Client, url: str, params: dict[str, Any]) -> list[dict[str, Any]] | None:
    """One request, retried once on a timeout. None when the answer is not a
    list of posts (an error, or a web page served instead of JSON)."""
    try:
        try:
            resp = client.get(url, params=params)
        except httpx.TimeoutException:
            resp = client.get(url, params=params)
    except httpx.HTTPError:
        return None
    if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
        return None
    try:
        posts = resp.json()
    except ValueError:
        return None
    return posts if isinstance(posts, list) else None


def _ask(client: httpx.Client, url: str, ids: list[str]) -> list[dict[str, Any]] | None:
    # orderby is not needed to select by id, but some installs answer `include`
    # with a cached web page unless the query looks like their own.
    # _fields is left off: Punch's Cloudflare challenges requests that carry it.
    return _get(client, url, {
        "include": ",".join(ids), "per_page": BATCH, "orderby": "modified", "order": "desc",
    })


def _fetch_source(source: dict[str, Any], ids: list[str]) -> dict[str, str]:
    """{source_article_id: text} for one newsroom."""
    url = source["endpoint"].rstrip("/") + "/posts"
    found: dict[str, str] = {}
    with httpx.Client(
        timeout=HTTP_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    ) as client:
        for start in range(0, len(ids), BATCH):
            chunk = ids[start:start + BATCH]
            posts = _ask(client, url, chunk)
            if posts is None and len(chunk) > SMALL_BATCH:
                # Refused as a batch: ask again a few at a time.
                posts = []
                for i in range(0, len(chunk), SMALL_BATCH):
                    posts += _ask(client, url, chunk[i:i + SMALL_BATCH]) or []
            for post in posts or []:
                text = body_text((post.get("content") or {}).get("rendered"))
                if text:
                    found[str(post.get("id"))] = text
    return found


def can_fetch(source: dict[str, Any]) -> bool:
    return source["adapter"] == "wordpress" and bool(source.get("full_text", 1))


def fetch_texts(rows: list[dict[str, Any]], sources: dict[str, Any]) -> dict[str, str | None]:
    """Full text for each article row, keyed by article id; None where the
    newsroom cannot be asked or did not answer."""
    now = time.monotonic()
    out: dict[str, str | None] = {}
    wanted: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        hit = _cache.get(row["id"])
        if hit and now - hit[0] < CACHE_TTL:
            out[row["id"]] = hit[1]
        elif can_fetch(sources[row["source_id"]]):
            wanted[row["source_id"]].append(row)
        else:
            out[row["id"]] = None

    def work(source_id: str) -> tuple[str, dict[str, str]]:
        ids = [r["source_article_id"] for r in wanted[source_id]]
        return source_id, _fetch_source(dict(sources[source_id]), ids)

    if wanted:
        with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
            for source_id, found in pool.map(work, list(wanted)):
                for row in wanted[source_id]:
                    text = found.get(row["source_article_id"])
                    out[row["id"]] = text
                    if text:
                        _cache[row["id"]] = (now, text)

    # Expired entries go, so the cache stays the size of recent downloads.
    for key in [k for k, (at, _) in _cache.items() if now - at >= CACHE_TTL]:
        del _cache[key]
    return out
