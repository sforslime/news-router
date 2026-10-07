from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..auth import authenticate
from ..serialize import article_out
from .common import decode_cursor, encode_cursor, sources_map

router = APIRouter()


@router.get("/v1/articles", summary="Unified cross-newsroom feed")
async def list_articles(
    request: Request,
    q: str | None = Query(None, min_length=2, description="Free text, matched as /v1/search matches it; results stay in date order"),
    source: str | None = Query(None, description="Comma-separated source ids"),
    section: str | None = None,
    language: str | None = None,
    wire_source: str | None = Query(None, description="e.g. NAN; use 'none' for original reporting"),
    since: str | None = Query(None, description="ISO timestamp, on published_at (UTC)"),
    until: str | None = None,
    retracted: bool | None = None,
    include_sponsored: bool = Query(False, description="Advertorial and syndicated PR are excluded by default"),
    limit: int = Query(25, ge=1, le=100),
    cursor: str | None = None,
    auth: dict = Depends(authenticate),
):
    conn = request.app.state.conn
    rows, next_cursor, total = feed(
        conn, q=q, source=source, section=section, language=language,
        wire_source=wire_source, since=since, until=until, retracted=retracted,
        include_sponsored=include_sponsored, limit=limit, cursor=cursor,
    )
    request.state.usage = {"results": total if total is not None else len(rows)}
    srcs = sources_map(request)

    out = {
        "count": len(rows),
        "has_more": next_cursor is not None,
        "next_cursor": next_cursor,
        "articles": [article_out(r, srcs[r["source_id"]]) for r in rows],
    }
    if total is not None:
        out["total"] = total
    return out


def feed(
    conn,
    *,
    q: str | None = None,
    source: str | None = None,
    section: str | None = None,
    language: str | None = None,
    wire_source: str | None = None,
    since: str | None = None,
    until: str | None = None,
    retracted: bool | None = None,
    include_sponsored: bool = False,
    limit: int = 25,
    cursor: str | None = None,
) -> tuple[list, str | None, int | None]:
    """One page of the feed, newest first: (rows, next_cursor, total). The
    total is counted only with a search term. Shared by the feed and the export."""
    where: list[str] = []
    params: dict[str, Any] = {}

    if q:
        where.append("a.search @@ websearch_to_tsquery('english', %(q)s)")
        params["q"] = q
    if source:
        ids = [s.strip() for s in source.split(",") if s.strip()]
        keys = [f"%(src{i})s" for i in range(len(ids))]
        where.append(f"a.source_id IN ({', '.join(keys)})")
        params.update({f"src{i}": v for i, v in enumerate(ids)})
    if section:
        where.append("a.section = %(section)s")
        params["section"] = section
    if language:
        where.append("a.language = %(language)s")
        params["language"] = language
    if wire_source:
        if wire_source.lower() == "none":
            where.append("a.wire_source IS NULL")
        else:
            where.append("a.wire_source = %(wire)s")
            params["wire"] = wire_source
    if since:
        where.append("a.published_at >= %(since)s")
        params["since"] = since
    if until:
        where.append("a.published_at <= %(until)s")
        params["until"] = until
    if retracted is not None:
        where.append("a.retracted = %(retracted)s")
        params["retracted"] = int(retracted)
    if not include_sponsored:
        where.append("a.sponsored = 0")
    # With a search term the total is worth knowing ("412 reports"), and the
    # text index makes counting cheap. Counted before the cursor narrows it.
    total = None
    if q:
        total = int(conn.execute(
            f"SELECT COUNT(*) AS n FROM articles a WHERE {' AND '.join(where)}", params
        ).fetchone()["n"])
    if cursor:
        c_pub, c_id = decode_cursor(cursor)
        where.append("(a.published_at, a.id) < (%(c_pub)s, %(c_id)s)")
        params.update({"c_pub": c_pub, "c_id": c_id})

    clause = f"WHERE {' AND '.join(where)}" if where else ""
    # Fetch one extra row to decide whether a next page exists.
    rows = conn.execute(
        f"""SELECT a.* FROM articles a {clause}
            ORDER BY a.published_at DESC, a.id DESC LIMIT %(limit)s""",
        {**params, "limit": limit + 1},
    ).fetchall()

    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = encode_cursor(rows[-1]["published_at"], rows[-1]["id"]) if has_more and rows else None
    return rows, next_cursor, total


@router.get("/v1/articles/{article_id}/revisions", summary="Edit history for one article")
async def article_revisions(article_id: str, request: Request, auth: dict = Depends(authenticate)):
    conn = request.app.state.conn
    article = conn.execute("SELECT * FROM articles WHERE id = %s", (article_id,)).fetchone()
    if article is None:
        raise HTTPException(404, f"No article {article_id!r}.")
    revs = conn.execute(
        "SELECT * FROM article_revisions WHERE article_id = %s ORDER BY revision ASC", (article_id,)
    ).fetchall()
    return {
        "article_id": article_id,
        "current_revision": article["revision"],
        "retracted": bool(article["retracted"]),
        "retraction_note": article["retraction_note"],
        "revisions": [
            {
                "revision": r["revision"],
                "seen_at": r["seen_at"],
                "headline": r["headline"],
                "changed_fields": json.loads(r["changed"] or "[]"),
                "content_hash": r["content_hash"],
            }
            for r in revs
        ],
    }


@router.get("/v1/articles/{article_id}", summary="One article")
async def get_article(article_id: str, request: Request, auth: dict = Depends(authenticate)):
    conn = request.app.state.conn
    row = conn.execute("SELECT * FROM articles WHERE id = %s", (article_id,)).fetchone()
    if row is None:
        raise HTTPException(404, f"No article {article_id!r}.")
    srcs = sources_map(request)
    return article_out(row, srcs[row["source_id"]])
