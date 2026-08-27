from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..auth import authenticate
from ..db import counts, get_state

router = APIRouter()


@router.get("/v1/health", summary="Liveness and corpus size")
async def health(request: Request):
    conn = request.app.state.conn
    stale = conn.execute(
        "SELECT id, last_error FROM sources WHERE enabled = 1 AND last_error IS NOT NULL"
    ).fetchall()
    return {
        "status": "degraded" if stale else "ok",
        "counts": counts(conn),
        "failing_sources": [dict(r) for r in stale],
        "gist_writer": get_state(conn, "gist_writer"),
    }


@router.get("/v1", summary="Endpoint index")
async def index(auth: dict = Depends(authenticate)):
    return {
        "name": "Nigerian news router",
        "version": "0.1.0",
        "endpoints": {
            "GET /v1/sources": "Newsrooms in the index, with tier and licensing state",
            "GET /v1/articles": "Unified feed across sources; filter and paginate",
            "GET /v1/articles/{id}": "One article",
            "GET /v1/articles/{id}/revisions": "Every observed edit, including corrections",
            "GET /v1/search": "Full-text search over headline, dek, snippet, entities",
            "GET /v1/search/gist": "Streamed gist of recent coverage on a topic (NDJSON)",
            "GET /v1/clusters": "Same story across outlets",
        },
        "rate_limit": {"plan": auth["plan"], "per_minute": auth["limit"], "remaining": auth["remaining"]},
        "notes": "Metadata only. Article bodies are never stored or served, at any tier.",
    }
