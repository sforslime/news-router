from __future__ import annotations

import secrets

from fastapi import APIRouter, Header, HTTPException

from .. import cluster, db, gist
from ..config import CRON_SECRET
from ..ingest import ingest_source

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


@router.get("/v1/admin/ingest", include_in_schema=False)
async def run_ingest(
    limit: int = 60,
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
        results = [
            ingest_source(conn, dict(source), limit=limit, since=None)
            for source in db.enabled_sources(conn)
        ]
        totals = db.counts(conn)

    failed = [r["source"] for r in results if r["error"]]
    return {
        "status": "degraded" if failed else "ok",
        "sources": results,
        "counts": totals,
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

    return {
        "status": "degraded" if gist_stats.get("status") == "degraded" else "ok",
        "clustering": cluster_stats,
        "gists": gist_stats,
    }
