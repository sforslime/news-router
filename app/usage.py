"""Record what is searched, downloaded and called, for the usage dashboard.

One row per request, written after the response has gone out, through a role
that can only INSERT into usage_events. Recording must never cost a reader
anything, so every failure here is swallowed.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from fastapi import Request

from . import db
from .config import CRON_SECRET, DATABASE_URL_USAGE
from .normalize import now_iso

log = logging.getLogger(__name__)

# Page furniture the front page fetches on every visit. The visit itself is
# recorded; these would only repeat it.
_FURNITURE = {"/v1/health", "/v1/sources"}

_conn: db.ServingConnection | None = None


def kind_of(path: str) -> str | None:
    """What a request was, or None when it is not worth recording."""
    if path == "/":
        return "visit"
    if not path.startswith("/v1") or path.startswith("/v1/admin"):
        return None
    if path == "/v1/search":
        return "search"
    if path == "/v1/search/gist":
        return "gist"
    if path == "/v1/articles":
        return "feed"
    if path == "/v1/export":
        return "export"
    return "api"


def visitor_of(request: Request) -> str:
    """A short salted hash of the caller's address: the same visitor gets the
    same value, and the address itself is never kept."""
    host = request.client.host if request.client else "unknown"
    return hashlib.sha256(f"{CRON_SECRET}|{host}".encode()).hexdigest()[:12]


def event(request: Request, status: int, ms: int) -> dict[str, Any] | None:
    path = request.url.path
    kind = kind_of(path)
    if kind is None:
        return None
    referrer = request.headers.get("referer") or ""
    # The front page's own furniture requests carry the page as referrer.
    if path in _FURNITURE and referrer.startswith(str(request.base_url)):
        return None
    usage = getattr(request.state, "usage", None) or {}
    return {
        "at": now_iso(),
        "kind": kind,
        "path": path,
        "params": json.dumps(dict(request.query_params), ensure_ascii=False),
        "visitor": visitor_of(request),
        "country": request.headers.get("x-vercel-ip-country"),
        "referrer": referrer[:300] or None,
        "agent": (request.headers.get("user-agent") or "")[:200] or None,
        "status": status,
        "results": usage.get("results"),
        "article_ids": json.dumps(usage["article_ids"]) if usage.get("article_ids") else None,
        "ms": ms,
    }


def write(row: dict[str, Any]) -> None:
    global _conn
    try:
        if _conn is None:
            _conn = db.ServingConnection(DATABASE_URL_USAGE)
        _conn.execute(
            """INSERT INTO usage_events (at, kind, path, params, visitor, country, referrer,
                                         agent, status, results, article_ids, ms)
               VALUES (%(at)s, %(kind)s, %(path)s, %(params)s, %(visitor)s, %(country)s,
                       %(referrer)s, %(agent)s, %(status)s, %(results)s, %(article_ids)s, %(ms)s)""",
            row,
        )
    except Exception as exc:  # recording never breaks a request
        log.warning("usage not recorded: %s", exc)
