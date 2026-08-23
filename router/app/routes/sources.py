from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from ..auth import authenticate
from ..serialize import source_out

router = APIRouter()


@router.get("/v1/sources", summary="List newsrooms")
async def list_sources(request: Request, enabled_only: bool = False, auth: dict = Depends(authenticate)):
    conn = request.app.state.conn
    sql = "SELECT * FROM sources"
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY tier, id"
    rows = conn.execute(sql).fetchall()
    return {"count": len(rows), "sources": [source_out(r) for r in rows]}


@router.get("/v1/sources/{source_id}", summary="One newsroom")
async def get_source(source_id: str, request: Request, auth: dict = Depends(authenticate)):
    conn = request.app.state.conn
    row = conn.execute("SELECT * FROM sources WHERE id = %s", (source_id,)).fetchone()
    if row is None:
        raise HTTPException(404, f"No source {source_id!r}.")
    stats = conn.execute(
        """SELECT COUNT(*) articles, MIN(published_at) earliest, MAX(published_at) latest
           FROM articles WHERE source_id = %s""",
        (source_id,),
    ).fetchone()
    return {**source_out(row), "stats": dict(stats)}
