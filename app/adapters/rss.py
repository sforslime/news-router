"""Tier 4: public RSS feed (usually /feed/).

The thinnest ingestion tier: whatever the outlet chose to syndicate — headline,
link, timestamp, a short description. Feeds exist to be read by aggregators,
which is why this tier ships with every rights flag off in the registry:
headline, link and attribution are served, nothing else.
"""
from __future__ import annotations

from typing import Any

import feedparser
import httpx

from ..config import HTTP_TIMEOUT, USER_AGENT
from ..normalize import normalize_rss
from .base import FetchError


class RSSAdapter:
    name = "rss"

    def fetch(self, source: dict[str, Any], limit: int = 50, since: str | None = None) -> list[dict[str, Any]]:
        # `since` is ignored: a feed only ever serves its current window, and
        # there is no way to ask it for less.
        try:
            resp = httpx.get(
                source["endpoint"],
                timeout=HTTP_TIMEOUT,
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml, text/xml"},
            )
        except httpx.HTTPError as exc:
            raise FetchError(f"{source['id']}: request failed: {exc}") from exc

        if resp.status_code == 403:
            raise FetchError(f"{source['id']}: 403 — feed is gated (Cloudflare)")
        if resp.status_code != 200:
            raise FetchError(f"{source['id']}: HTTP {resp.status_code}")

        feed = feedparser.parse(resp.content)
        if not feed.entries:
            # bozo alone is not fatal — feedparser recovers from most malformed
            # XML — but a feed that parsed to nothing is.
            detail = getattr(feed, "bozo_exception", None)
            raise FetchError(f"{source['id']}: feed yielded no entries" + (f" ({detail})" if detail else ""))

        collected: list[dict[str, Any]] = []
        seen: set[str] = set()
        for entry in feed.entries:
            if len(collected) >= limit:
                break
            rec = normalize_rss(entry, source["id"])
            if rec["id"] in seen:
                continue
            if rec["headline"] and rec["published_at"] and rec["canonical_url"]:
                seen.add(rec["id"])
                collected.append(rec)
        return collected
