from __future__ import annotations

import base64
from typing import Any

from fastapi import HTTPException, Request


def sources_map(request: Request) -> dict[str, Any]:
    conn = request.app.state.conn
    return {r["id"]: r for r in conn.execute("SELECT * FROM sources").fetchall()}


def encode_cursor(published_at: str, article_id: str) -> str:
    return base64.urlsafe_b64encode(f"{published_at}|{article_id}".encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        published_at, article_id = base64.urlsafe_b64decode(padded).decode().split("|", 1)
        return published_at, article_id
    except Exception:
        raise HTTPException(400, "Malformed cursor.")
