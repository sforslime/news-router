from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..auth import authenticate
from ..serialize import article_out
from .common import sources_map

router = APIRouter()


@router.get("/v1/clusters", summary="Same story, multiple outlets")
async def list_clusters(
    request: Request,
    min_size: int = Query(2, ge=1, description="Only clusters carried by at least this many articles"),
    limit: int = Query(25, ge=1, le=100),
    auth: dict = Depends(authenticate),
):
    conn = request.app.state.conn
    rows = conn.execute(
        """SELECT * FROM clusters WHERE size >= :min_size
           ORDER BY last_published_at DESC LIMIT :limit""",
        {"min_size": min_size, "limit": limit},
    ).fetchall()
    return {
        "count": len(rows),
        "clusters": [
            {
                "id": r["id"],
                "label": r["label"],
                "size": r["size"],
                "first_published_at": r["first_published_at"],
                "last_published_at": r["last_published_at"],
            }
            for r in rows
        ],
    }


@router.get("/v1/clusters/{cluster_id}", summary="Every outlet's version of one story")
async def get_cluster(cluster_id: str, request: Request, auth: dict = Depends(authenticate)):
    conn = request.app.state.conn
    cluster = conn.execute("SELECT * FROM clusters WHERE id = ?", (cluster_id,)).fetchone()
    if cluster is None:
        raise HTTPException(404, f"No cluster {cluster_id!r}.")
    rows = conn.execute(
        "SELECT * FROM articles WHERE cluster_id = ? ORDER BY published_at ASC", (cluster_id,)
    ).fetchall()
    srcs = sources_map(request)
    return {
        "id": cluster["id"],
        "label": cluster["label"],
        "size": cluster["size"],
        "first_published_at": cluster["first_published_at"],
        "last_published_at": cluster["last_published_at"],
        "coverage": [article_out(r, srcs[r["source_id"]]) for r in rows],
    }
