from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..auth import authenticate
from ..serialize import article_out
from .common import sources_map

router = APIRouter()


def _gist_out(row, srcs) -> dict | None:
    """Shape a stored gist for a response. The per-outlet notes are keyed by
    source_id in storage; attribution is attached here so a consumer never has
    to join against /v1/sources to credit an outlet."""
    if row is None or row["summary"] is None:
        return None
    notes = []
    for note in json.loads(row["coverage"] or "[]"):
        source = srcs.get(note.get("source_id"))
        notes.append(
            {
                "source_id": note.get("source_id"),
                "outlet": source["attribution_name"] if source else note.get("source_id"),
                "note": note.get("note"),
            }
        )
    return {
        "summary": row["summary"],
        "notes": notes,
        "model": row["model"],
        "generated_at": row["generated_at"],
    }


@router.get("/v1/clusters", summary="Same story, multiple outlets")
async def list_clusters(
    request: Request,
    min_size: int = Query(2, ge=1, description="Only clusters carried by at least this many articles"),
    limit: int = Query(25, ge=1, le=100),
    auth: dict = Depends(authenticate),
):
    conn = request.app.state.conn
    rows = conn.execute(
        """SELECT c.*, g.summary AS gist_summary
           FROM clusters c LEFT JOIN cluster_gists g ON g.cluster_id = c.id
           WHERE c.size >= %(min_size)s
           ORDER BY c.last_published_at DESC LIMIT %(limit)s""",
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
                "gist": r["gist_summary"],
            }
            for r in rows
        ],
    }


@router.get("/v1/clusters/{cluster_id}", summary="One story: its gist and every outlet's version")
async def get_cluster(cluster_id: str, request: Request, auth: dict = Depends(authenticate)):
    conn = request.app.state.conn
    cluster = conn.execute("SELECT * FROM clusters WHERE id = %s", (cluster_id,)).fetchone()
    if cluster is None:
        raise HTTPException(404, f"No cluster {cluster_id!r}.")
    rows = conn.execute(
        "SELECT * FROM articles WHERE cluster_id = %s ORDER BY published_at ASC", (cluster_id,)
    ).fetchall()
    gist_row = conn.execute(
        "SELECT * FROM cluster_gists WHERE cluster_id = %s", (cluster_id,)
    ).fetchone()
    srcs = sources_map(request)
    return {
        "id": cluster["id"],
        "label": cluster["label"],
        "size": cluster["size"],
        "first_published_at": cluster["first_published_at"],
        "last_published_at": cluster["last_published_at"],
        "gist": _gist_out(gist_row, srcs),
        "coverage": [article_out(r, srcs[r["source_id"]]) for r in rows],
    }
