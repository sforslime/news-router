"""Every report on a topic as Markdown, with full text fetched live.

Paged like the feed, so each response stays small and quick however many
reports match: the caller follows X-Next-Cursor and joins the pages. The
website's download button and `python -m app.export` both do exactly that,
so the file's layout is decided here and nowhere else.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse

from ..auth import authenticate
from ..fulltext import fetch_texts
from .articles import feed
from .common import sources_map

router = APIRouter()

PAGE = 50
SITE = "https://news-router.vercel.app"
LAGOS = timezone(timedelta(hours=1))


def _lagos(iso: str | None) -> str:
    """'2026-10-06T13:05:00Z' -> '6 Oct 2026, 14:05 WAT'."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(LAGOS)
    return f"{dt.day} {dt:%b %Y, %H:%M} WAT"


def _day(iso: str | None) -> str:
    return _lagos(iso).split(",")[0] if iso else ""


def header(q: str, since: str | None, until: str | None, total: int) -> str:
    if since and until:
        span = f"published between {_day(since)} and {_day(until)}"
    elif since:
        span = f"published since {_day(since)}"
    elif until:
        span = f"published up to {_day(until)}"
    else:
        span = "in the index"
    now = _lagos(datetime.now(timezone.utc).isoformat())
    return (
        f"# “{q}”: Nigerian News Router\n\n"
        f"{total:,} report{'' if total == 1 else 's'} {span}, newest first. "
        f"Exported {now} from {SITE}.\n\n"
        "Full text is fetched live from each newsroom's own site, and every report "
        "links to its original. Some newsrooms only publish a short feed; their "
        "reports carry the summary and the link.\n\n---\n\n"
    )


def article_md(row, source, text: str | None) -> str:
    meta = [f"**{source['name']}**"]
    if row["byline"]:
        meta.append(f"By {row['byline']}")
    meta.append(_lagos(row["published_at"]))
    meta.append(f"[Original]({row['canonical_url']})")
    parts = [f"## {row['headline']}", " · ".join(meta)]
    if text:
        parts.append(text)
    else:
        summary = row["dek"] or row["snippet"]
        if summary:
            parts.append(summary)
        parts.append(f"*Full text not available here. Read it at {source['name']}: {row['canonical_url']}*")
    return "\n\n".join(parts) + "\n\n---\n\n"


@router.get(
    "/v1/export",
    summary="Every report on a topic as Markdown, full text fetched live",
    response_class=PlainTextResponse,
)
async def export(
    request: Request,
    q: str = Query(..., min_length=2, description="The topic, as you would search it"),
    since: str | None = Query(None, description="ISO timestamp, on published_at (UTC)"),
    until: str | None = None,
    source: str | None = Query(None, description="Comma-separated source ids"),
    cursor: str | None = Query(None, description="From the previous page's X-Next-Cursor header"),
    auth: dict = Depends(authenticate),
):
    conn = request.app.state.conn
    rows, next_cursor, total = feed(
        conn, q=q, source=source, since=since, until=until, limit=PAGE, cursor=cursor
    )
    srcs = sources_map(request)
    texts = fetch_texts([dict(r) for r in rows], srcs)

    body = header(q, since, until, total or 0) if not cursor else ""
    body += "".join(article_md(r, srcs[r["source_id"]], texts.get(r["id"])) for r in rows)

    request.state.usage = {
        "results": len(rows),
        "article_ids": [r["id"] for r in rows],
    }
    headers = {"X-Total": str(total or 0), "X-Count": str(len(rows)),
               "X-Full-Text": str(sum(1 for r in rows if texts.get(r["id"])))}
    if next_cursor:
        headers["X-Next-Cursor"] = next_cursor
    return PlainTextResponse(body, media_type="text/markdown; charset=utf-8", headers=headers)
