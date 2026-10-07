from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from .. import gist
from ..auth import authenticate, gist_allowance
from ..serialize import article_out
from .common import sources_map

router = APIRouter()

# One topic gist is one model call, so it is cached on which articles it read,
# not on the words typed: "nysc" and "nysc camp" matching the same reports share
# one summary. The index only moves at the morning read, so an hour is safe.
# Per warm process only — the serving path holds a read-only database role and
# cannot cache anywhere durable, by design.
_CACHE_TTL = 60 * 60
_CACHE_MAX = 100
_gist_cache: dict[str, tuple[float, str, str]] = {}  # input hash -> (expires, text, model)

# Enough coverage to be worth a summary, few enough articles to stay a gist.
# Up to MATCH_POOL matches are grouped by story; the strongest story is written
# up in full, and a few others are mentioned by headline only.
MIN_TOPIC_ARTICLES = 2
MATCH_POOL = 40
MAX_RELATED = 4


@router.get("/v1/search", summary="Full-text search across every indexed newsroom")
async def search(
    request: Request,
    q: str = Query(..., min_length=2, description="Free text; matches headline, dek, snippet, entities"),
    source: str | None = None,
    since: str | None = Query(None, description="ISO timestamp, on published_at (UTC)"),
    until: str | None = None,
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
    if since:
        sql += " AND a.published_at >= %(since)s"
        params["since"] = since
    if until:
        sql += " AND a.published_at <= %(until)s"
        params["until"] = until
    if not include_sponsored:
        sql += " AND a.sponsored = 0"
    # Rank first, then recency, so a strong old match still beats a weak new one.
    sql += " ORDER BY score DESC, a.published_at DESC LIMIT %(limit)s"

    rows = conn.execute(sql, params).fetchall()
    request.state.usage = {"results": len(rows)}
    srcs = sources_map(request)
    return {
        "query": q,
        "count": len(rows),
        "articles": [
            {**article_out(r, srcs[r["source_id"]]), "score": round(float(r["score"]), 4)}
            for r in rows
        ],
    }


def _topic_rows(conn, q: str, days: int) -> list:
    """The articles a topic gist chooses from: same match as /v1/search,
    restricted to the recent window, advertorial and retractions excluded."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat().replace("+00:00", "Z")
    return conn.execute(
        """SELECT a.*, ts_rank_cd(a.search, query) AS score
           FROM articles a, websearch_to_tsquery('english', %(q)s) AS query
           WHERE a.search @@ query AND a.published_at >= %(cutoff)s
             AND a.sponsored = 0 AND a.retracted = 0
           ORDER BY score DESC, a.published_at DESC LIMIT %(cap)s""",
        {"q": q, "cutoff": cutoff, "cap": MATCH_POOL},
    ).fetchall()


def _score(r) -> float:
    return float(r.get("score") or 0)


def _pick_story(rows: list) -> tuple[list, list]:
    """(main, related). Matches are grouped by the story the router already put
    them in; the group with the most matching weight is the main story. Each
    other group contributes its best-matching report, by headline only, so a
    loose word match cannot get blended into the main story."""
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r.get("cluster_id") or f"solo:{r['id']}", []).append(r)
    ranked = sorted(groups.values(), key=lambda g: sum(_score(r) for r in g), reverse=True)
    if not ranked:
        return [], []
    main = gist.pick_articles(ranked[0])
    related = [max(g, key=_score) for g in ranked[1:1 + MAX_RELATED]]
    return main, related


def _describe_main(main: list, sources: dict) -> dict | None:
    """What the page needs to dress the summary as a story card. Only fields
    the API already serves on every article."""
    if not main:
        return None
    name = lambda r: sources[r["source_id"]]["attribution_name"]
    lead = max(main, key=_score)
    # The lead report's own photo first, so it matches the headline; else the newest.
    with_image = sorted((r for r in main if r.get("image")),
                        key=lambda r: (r is lead, r["published_at"] or ""), reverse=True)
    return {
        "headline": lead["headline"],
        "image": with_image[0]["image"] if with_image else None,
        "image_source": name(with_image[0]) if with_image else None,
        "outlets": list(dict.fromkeys(name(r) for r in main)),
        "latest": max(r["published_at"] or "" for r in main) or None,
    }


def _ndjson(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False) + "\n"


def _gist_lines(q: str, days: int, rows: list, sources: dict, identity: str = "local"):
    main, related = _pick_story(rows)
    used = main + related
    yield _ndjson({"type": "meta", "query": q, "days": days,
                   "articles": len(used), "newsrooms": len({r["source_id"] for r in used}),
                   "main_articles": len(main),
                   "main_newsrooms": len({r["source_id"] for r in main}),
                   "related": len(related),
                   "main": _describe_main(main, sources)})

    if len(used) < MIN_TOPIC_ARTICLES:
        yield _ndjson({"type": "status", "status": "too little",
                       "message": "Not enough recent reporting on this to write a gist."})
        return

    key = gist.input_hash(used)
    cached = _gist_cache.get(key)
    if cached and cached[0] > time.monotonic():
        yield _ndjson({"type": "delta", "text": cached[1]})
        yield _ndjson({"type": "done", "model": cached[2], "cached": True})
        return

    writer = gist.stream_writer()
    if isinstance(writer, dict):
        yield _ndjson({"type": "status", "status": writer["status"], "message": writer["detail"]})
        return
    stream_fn, model_tag = writer

    if not gist_allowance(identity):
        yield _ndjson({"type": "status", "status": "busy",
                       "message": "Summaries are paused for a bit."})
        return

    prompt = gist.build_topic_prompt(q, main, sources, related)
    parts: list[str] = []
    try:
        for delta in stream_fn(gist.TOPIC_SYSTEM, prompt):
            parts.append(delta)
            yield _ndjson({"type": "delta", "text": delta})
    except Exception as exc:
        yield _ndjson({"type": "status", "status": "error", "message": str(exc)})
        return

    if len(_gist_cache) >= _CACHE_MAX:
        _gist_cache.pop(next(iter(_gist_cache)))
    _gist_cache[key] = (time.monotonic() + _CACHE_TTL, "".join(parts), model_tag)
    yield _ndjson({"type": "done", "model": model_tag, "cached": False})


@router.get("/v1/search/gist", summary="Streamed gist of recent coverage on a topic")
async def search_gist(
    request: Request,
    q: str = Query(..., min_length=2, max_length=120, description="The topic, as you would search it"),
    days: int = Query(7, ge=1, le=30, description="How far back 'recently' reaches"),
    auth: dict = Depends(authenticate),
):
    """NDJSON stream: a meta line, then delta lines as the summary is written,
    then done — or a single status line when there is nothing to write with
    (too little coverage, or no gist writer available right now)."""
    conn = request.app.state.conn
    rows = _topic_rows(conn, q, days)
    srcs = sources_map(request)
    return StreamingResponse(
        _gist_lines(q, days, rows, srcs, auth["identity"]),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
