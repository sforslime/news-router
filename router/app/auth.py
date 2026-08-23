"""API-key auth and rate limiting.

The limiter is in-process, so it is per-instance. On Vercel, where several
function instances serve concurrently, move the counter to Redis/Upstash before
the limits are load-bearing for billing.
"""
from __future__ import annotations

import hashlib
import secrets
import time
from collections import defaultdict, deque
from typing import Any

from fastapi import Header, HTTPException, Request

from .config import ALLOW_ANON, ANON_RATE_PER_MIN, DEFAULT_RATE_PER_MIN
from .normalize import now_iso

_WINDOW = 60.0
_hits: dict[str, deque[float]] = defaultdict(deque)


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def mint_key(conn, name: str, plan: str = "free", rate: int = DEFAULT_RATE_PER_MIN) -> str:
    raw = "nr_" + secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO api_keys (key_hash, name, plan, rate_per_min, enabled, created_at) VALUES (?,?,?,?,1,?)",
        (hash_key(raw), name, plan, rate, now_iso()),
    )
    conn.commit()
    return raw  # shown once; only the hash is stored


def _consume(identity: str, limit: int) -> tuple[bool, int]:
    now = time.monotonic()
    bucket = _hits[identity]
    while bucket and now - bucket[0] > _WINDOW:
        bucket.popleft()
    if len(bucket) >= limit:
        return False, 0
    bucket.append(now)
    return True, limit - len(bucket)


async def authenticate(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    raw = x_api_key
    if not raw and authorization and authorization.lower().startswith("bearer "):
        raw = authorization[7:].strip()

    conn = request.app.state.conn

    if not raw:
        if not ALLOW_ANON:
            raise HTTPException(401, "API key required. Send X-API-Key or Authorization: Bearer.")
        identity, limit, plan, name = f"anon:{request.client.host if request.client else 'unknown'}", ANON_RATE_PER_MIN, "anon", "anonymous"
    else:
        row = conn.execute(
            "SELECT * FROM api_keys WHERE key_hash = ? AND enabled = 1", (hash_key(raw),)
        ).fetchone()
        if row is None:
            raise HTTPException(401, "Invalid or disabled API key.")
        identity, limit, plan, name = f"key:{row['key_hash'][:16]}", int(row["rate_per_min"]), row["plan"], row["name"]

    allowed, remaining = _consume(identity, limit)
    if not allowed:
        raise HTTPException(
            429,
            f"Rate limit exceeded: {limit} requests/minute for plan '{plan}'.",
            headers={"Retry-After": "60", "X-RateLimit-Limit": str(limit), "X-RateLimit-Remaining": "0"},
        )

    return {"identity": identity, "plan": plan, "name": name, "limit": limit, "remaining": remaining}
