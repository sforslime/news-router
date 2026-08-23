from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from ..auth import authenticate
from ..serialize import article_out
from .common import sources_map

router = APIRouter()


@router.get("/v1/search", summary="Full-text search across every indexed newsroom")
async def search(
    request: Request,
    q: str = Query(..., min_length=2, description="Free text; matches headline, dek, snippet, entities"),
    source: str | None = None,
    include_sponsored: bool = Query(False, description="Advertorial and syndicated PR are excluded by default"),
    limit: int = Query(25, ge=1, le=100),
    auth: dict = Depends(authenticate),
):
    conn = request.app.state.conn

    # websearch_to_tsquery parses what people actually type — bare words,
    # "quoted phrases", OR, leading minus — and cannot be made to throw by
    # stray punctuation, so no sanitising pass is needed before it.
    sql = """
        SELECT a.*, ts_rank_cd(a.search, query) AS score
        FROM articles a, websearch_to_tsquery('english', %(q)s) AS query
        WHERE a.search @@ query
    """
    params: dict = {"q": q, "limit": limit}
    if source:
        ids = [s.strip() for s in source.split(",") if s.strip()]
        keys = [f"%(src{i})s" for i in range(len(ids))]
        sql += f" AND a.source_id IN ({', '.join(keys)})"
        params.update({f"src{i}": v for i, v in enumerate(ids)})
    if not include_sponsored:
        sql += " AND a.sponsored = 0"
    # Rank first, then recency, so a strong old match still beats a weak new one.
    sql += " ORDER BY score DESC, a.published_at DESC LIMIT %(limit)s"

    rows = conn.execute(sql, params).fetchall()
    srcs = sources_map(request)
    return {
        "query": q,
        "count": len(rows),
        "articles": [
            {**article_out(r, srcs[r["source_id"]]), "score": round(float(r["score"]), 4)}
            for r in rows
        ],
    }
