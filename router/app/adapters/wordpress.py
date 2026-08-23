"""Tier 2: open WordPress REST (/wp-json/wp/v2/posts).

Consumed only for sources whose registry entry is enabled. `_embed=1` pulls
author, featured image and taxonomy terms in the same round trip.
"""
from __future__ import annotations

from typing import Any

import httpx

from ..config import HTTP_TIMEOUT, USER_AGENT
from ..normalize import normalize_wordpress
from .base import FetchError

PAGE_SIZE = 50


class WordPressAdapter:
    name = "wordpress"

    def fetch(self, source: dict[str, Any], limit: int = 50, since: str | None = None) -> list[dict[str, Any]]:
        endpoint = source["endpoint"].rstrip("/") + "/posts"
        collected: list[dict[str, Any]] = []
        seen: set[str] = set()
        page = 1
        # Page size stays fixed for the whole walk. Shrinking it on later pages
        # re-shifts WordPress's offset and re-serves rows already collected.
        page_size = min(PAGE_SIZE, limit)

        with httpx.Client(
            timeout=HTTP_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        ) as client:
            while len(collected) < limit:
                params: dict[str, Any] = {
                    "per_page": page_size,
                    "page": page,
                    "_embed": 1,
                    # Order by modified so edits and corrections resurface, not just
                    # brand-new posts. This is what feeds revision tracking.
                    "orderby": "modified",
                    "order": "desc",
                }
                if since:
                    params["modified_after"] = since.replace("Z", "")

                try:
                    resp = client.get(endpoint, params=params)
                except httpx.HTTPError as exc:
                    raise FetchError(f"{source['id']}: request failed: {exc}") from exc

                if resp.status_code == 400 and page > 1:
                    break  # WordPress returns 400 past the last page
                if resp.status_code == 403:
                    raise FetchError(f"{source['id']}: 403 — endpoint is gated (Cloudflare or REST disabled)")
                if resp.status_code != 200:
                    raise FetchError(f"{source['id']}: HTTP {resp.status_code}")

                posts = resp.json()
                if not isinstance(posts, list) or not posts:
                    break

                for post in posts:
                    rec = normalize_wordpress(post, source["id"])
                    if rec["id"] in seen:
                        continue
                    if rec["headline"] and rec["published_at"] and rec["canonical_url"]:
                        seen.add(rec["id"])
                        collected.append(rec)

                total_pages = int(resp.headers.get("X-WP-TotalPages") or 0)
                if total_pages and page >= total_pages:
                    break
                page += 1

        return collected[:limit]
