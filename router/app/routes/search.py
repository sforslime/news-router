from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Query, Request

from ..auth import authenticate
from ..serialize import article_out
from .common import sources_map

router = APIRouter()

# FTS5 treats these as operators; callers are sending prose, not query syntax.
_FTS_SPECIAL = re.compile(r'["\'()*:^-]')


def to_fts_query(q: str) -> str:
    cleaned = _FTS_SPECIAL.sub(" ", q).strip()
    terms = [t for t in cleaned.split() if t]
    if not terms:
        return ""
    # Quote each term so multi-word names match as a conjunction, not as syntax.
    return " AND ".join(f'"{t}"' for t in terms)


@router.get("/v1/search", summary="Full-text search across every indexed newsroom")
async def search(
    request: Request,
    q: str = Query(..., min_length=2, description="Free text; matches headline, dek, snippet, entities"),
    source: str | None = None,
    limit: int = Query(25, ge=1, le=100),
    auth: dict = Depends(authenticate),
):
    conn = request.app.state.conn
    fts_query = to_fts_query(q)
    if not fts_query:
        return {"query": q, "count": 0, "articles": []}

    sql = """
        SELECT a.*, bm25(articles_fts) AS score
        FROM articles_fts
        JOIN articles a ON a.id = articles_fts.article_id
        WHERE articles_fts MATCH :q
    """
    params: dict = {"q": fts_query, "limit": limit}
    if source:
        ids = [s.strip() for s in source.split(",") if s.strip()]
        keys = [f":src{i}" for i in range(len(ids))]
        sql += f" AND a.source_id IN ({', '.join(keys)})"
        params.update({f"src{i}": v for i, v in enumerate(ids)})
    # bm25 returns negative numbers, smaller is a better match.
    sql += " ORDER BY score ASC LIMIT :limit"

    rows = conn.execute(sql, params).fetchall()
    srcs = sources_map(request)
    return {
        "query": q,
        "count": len(rows),
        "articles": [{**article_out(r, srcs[r["source_id"]]), "score": round(-r["score"], 4)} for r in rows],
    }
