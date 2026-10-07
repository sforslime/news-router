from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Header, HTTPException, Query, Request

from .. import cluster, db, gist
from ..config import ADMIN_TOKEN, CRON_SECRET
from ..digest import _state_of
from ..ingest import ingest_all

router = APIRouter()


def _authorise(header: str | None) -> None:
    """Only Vercel Cron may run this.

    Vercel sends `Authorization: Bearer $CRON_SECRET` on scheduled invocations.
    Comparison is constant-time so a wrong guess leaks nothing through timing,
    and an unset secret refuses everyone rather than admitting everyone.
    """
    if not CRON_SECRET:
        raise HTTPException(503, "Ingestion is not configured on this deployment.")
    expected = f"Bearer {CRON_SECRET}"
    if not header or not secrets.compare_digest(header, expected):
        raise HTTPException(401, "Not authorised.")


def _since(source) -> str | None:
    """Where to resume reading a newsroom: its last clean read, less a margin.

    Busy outlets publish 150+ reports a day, so walking back the full limit on
    every run is slow. After a failed run, or the first one, read the full limit.
    """
    last = source["last_ingest_at"]
    if not last or source["last_error"]:
        return None
    resume = datetime.fromisoformat(last.replace("Z", "+00:00")) - timedelta(hours=2)
    return resume.strftime("%Y-%m-%dT%H:%M:%SZ")


@router.get("/v1/admin/ingest", include_in_schema=False)
async def run_ingest(
    limit: int = 200,
    full: bool = False,
    authorization: str | None = Header(None),
):
    """Fetch each enabled newsroom. Called on a schedule, not by hand.

    This is the only write path in the deployed application, and it opens its
    own connection — app.state.conn is read-only and stays that way.
    """
    _authorise(authorization)

    with db.connect() as conn:
        db.init_db(conn)
        db.sync_sources(conn)
        sources = [dict(s) for s in db.enabled_sources(conn)]
        # full=1 ignores each outlet's resume point: a one-off re-read, e.g. to
        # fill in fields added after reports were first collected.
        results = ingest_all(conn, sources, limit, (lambda _: None) if full else _since)
        purged = db.purge_leads(conn)
        totals = db.counts(conn)

    failed = [r["source"] for r in results if r["error"]]
    return {
        "status": "degraded" if failed else "ok",
        "sources": results,
        "counts": totals,
        "leads_purged": purged,
    }


@router.get("/v1/admin/digest", include_in_schema=False)
async def run_digest(
    max_gists: int = 25,
    authorization: str | None = Header(None),
):
    """Cluster the recent window, then write story gists. Scheduled after
    ingest so the morning's articles are grouped and summarised in one pass;
    same authorisation and same private write connection as ingest."""
    _authorise(authorization)

    with db.connect() as conn:
        db.init_db(conn)
        cluster_stats = cluster.run(conn)
        gist_stats = gist.generate(conn, max_gists=max_gists)
        db.set_state(conn, "gist_writer", _state_of(gist_stats))

    return {
        "status": "degraded" if gist_stats.get("status") == "degraded" else "ok",
        "clustering": cluster_stats,
        "gists": gist_stats,
    }


@router.get("/v1/admin/usage", include_in_schema=False)
async def usage_report(
    request: Request,
    days: int = Query(7, ge=1, le=90),
    authorization: str | None = Header(None),
):
    """What the usage dashboard shows: activity per day, what people search
    for and download, where they are, and the latest requests. Reads through
    the serving connection; ADMIN_TOKEN opens it."""
    if not ADMIN_TOKEN:
        raise HTTPException(503, "The usage dashboard is not configured on this deployment.")
    if not authorization or not secrets.compare_digest(authorization, f"Bearer {ADMIN_TOKEN}"):
        raise HTTPException(401, "Not authorised.")

    conn = request.app.state.conn
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    p = {"cutoff": cutoff}
    # A download is the first page of an export; later pages carry a cursor.
    first_export = "kind = 'export' AND NOT (params::jsonb ? 'cursor')"
    # Searches are typed queries: the search box, a dated search's first page,
    # and the API's search endpoint. Show-more pages are not new searches.
    searched = "(kind = 'search' OR (kind = 'feed' AND params::jsonb ? 'q' AND NOT (params::jsonb ? 'cursor')))"

    def rows(sql: str) -> list[dict]:
        return [dict(r) for r in conn.execute(sql, p).fetchall()]

    return {
        "days": days,
        "since": cutoff,
        "totals": rows(f"""
            SELECT COUNT(DISTINCT visitor) AS visitors,
                   COUNT(*) FILTER (WHERE kind = 'visit') AS visits,
                   COUNT(*) FILTER (WHERE {searched}) AS searches,
                   COUNT(*) FILTER (WHERE {first_export}) AS downloads,
                   COALESCE(SUM(results) FILTER (WHERE kind = 'export'), 0) AS reports_downloaded,
                   COUNT(*) FILTER (WHERE kind = 'gist') AS summaries,
                   COUNT(*) FILTER (WHERE kind = 'api') AS api_calls
            FROM usage_events WHERE at >= %(cutoff)s""")[0],
        "by_day": rows(f"""
            SELECT substr(at, 1, 10) AS day,
                   COUNT(DISTINCT visitor) AS visitors,
                   COUNT(*) FILTER (WHERE kind = 'visit') AS visits,
                   COUNT(*) FILTER (WHERE {searched}) AS searches,
                   COUNT(*) FILTER (WHERE {first_export}) AS downloads
            FROM usage_events WHERE at >= %(cutoff)s
            GROUP BY 1 ORDER BY 1 DESC"""),
        "top_searches": rows(f"""
            SELECT lower(params::jsonb ->> 'q') AS q, COUNT(*) AS times,
                   COUNT(DISTINCT visitor) AS visitors, MAX(at) AS last
            FROM usage_events
            WHERE at >= %(cutoff)s AND ({searched} OR kind = 'gist')
              AND params::jsonb ? 'q'
            GROUP BY 1 ORDER BY times DESC, last DESC LIMIT 25"""),
        "downloads": rows("""
            SELECT lower(params::jsonb ->> 'q') AS q, params::jsonb ->> 'since' AS since,
                   params::jsonb ->> 'until' AS until, COUNT(DISTINCT visitor) AS visitors,
                   COUNT(*) FILTER (WHERE NOT (params::jsonb ? 'cursor')) AS times,
                   SUM(results) AS reports, MAX(at) AS last
            FROM usage_events WHERE at >= %(cutoff)s AND kind = 'export'
            GROUP BY 1, 2, 3 ORDER BY last DESC LIMIT 25"""),
        "countries": rows("""
            SELECT COALESCE(country, '?') AS country, COUNT(DISTINCT visitor) AS visitors,
                   COUNT(*) AS requests
            FROM usage_events WHERE at >= %(cutoff)s
            GROUP BY 1 ORDER BY visitors DESC, requests DESC LIMIT 20"""),
        "recent": rows("""
            SELECT at, kind, path, params, visitor, country, referrer, status, results, ms
            FROM usage_events WHERE at >= %(cutoff)s
            ORDER BY at DESC LIMIT 60"""),
    }
