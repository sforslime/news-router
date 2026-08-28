from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..auth import authenticate
from ..db import get_state
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


def _writer_out(conn) -> dict:
    """The gist writer's last recorded outcome, for callers wondering why a
    story has no gist yet. The writer may live on a laptop that is allowed to
    be closed; that is a state worth reporting, not hiding."""
    state = get_state(conn, "gist_writer")
    if state is None:
        return {"status": "never run", "detail": "No digest has run yet."}
    return {k: state.get(k) for k in ("status", "detail", "model", "at")}


def _missing_gist_note(writer: dict) -> str:
    status = writer.get("status")
    if status == "offline":
        return f"No gist yet: the local model was offline at the last run ({writer.get('at')})."
    if status == "not configured":
        return "No gist yet: no gist writer is configured."
    if status == "never run":
        return "No gist yet: the gist writer has not run."
    return "No gist yet: this coverage arrived after the last run; the next one writes it."


# Sort keys are whitelisted rather than interpolated: the value reaches an
# ORDER BY, which takes no placeholder.
_CLUSTER_SORTS = {
    "recent": "c.last_published_at DESC",
    # Recency breaks the tie, so "the biggest story" stays stable between calls
    # instead of shuffling whenever two clusters are the same size.
    "size": "c.size DESC, c.last_published_at DESC",
}


@router.get("/v1/clusters", summary="Same story, multiple outlets")
async def list_clusters(
    request: Request,
    min_size: int = Query(2, ge=1, description="Only clusters carried by at least this many articles"),
    limit: int = Query(25, ge=1, le=100),
    sort: str = Query("recent", description="'recent' — newest first; 'size' — most-covered first"),
    hours: int | None = Query(None, ge=1, description="Only clusters last published within this many hours"),
    auth: dict = Depends(authenticate),
):
    if sort not in _CLUSTER_SORTS:
        raise HTTPException(400, f"sort must be one of: {', '.join(sorted(_CLUSTER_SORTS))}.")

    params: dict = {"min_size": min_size, "limit": limit}
    window = ""
    if hours is not None:
        # Timestamps are stored as ISO-8601 UTC text, which compares correctly
        # as text — same trick the clustering window uses.
        params["cutoff"] = (
            datetime.now(timezone.utc) - timedelta(hours=hours)
        ).isoformat().replace("+00:00", "Z")
        window = "AND c.last_published_at >= %(cutoff)s"

    conn = request.app.state.conn
    rows = conn.execute(
        f"""SELECT c.*, g.summary AS gist_summary
           FROM clusters c LEFT JOIN cluster_gists g ON g.cluster_id = c.id
           WHERE c.size >= %(min_size)s {window}
           ORDER BY {_CLUSTER_SORTS[sort]} LIMIT %(limit)s""",
        params,
    ).fetchall()
    return {
        "count": len(rows),
        "gist_writer": _writer_out(conn),
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
    gist = _gist_out(gist_row, srcs)
    out = {
        "id": cluster["id"],
        "label": cluster["label"],
        "size": cluster["size"],
        "first_published_at": cluster["first_published_at"],
        "last_published_at": cluster["last_published_at"],
        "gist": gist,
        "coverage": [article_out(r, srcs[r["source_id"]]) for r in rows],
    }
    if gist is None:
        out["gist_status"] = _missing_gist_note(_writer_out(conn))
    return out
