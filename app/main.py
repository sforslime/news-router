from __future__ import annotations

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from . import db, usage
from .config import APP_DIR
from .routes import admin, articles, clusters, export, meta, search, sources

STATIC_DIR = APP_DIR / "static"

DESCRIPTION = """
One API across Nigerian newsrooms.

Returns headline, dek, byline, timestamp, canonical URL, snippet and thumbnail
for every report, read from the feeds and site endpoints the newsrooms publish
openly. `/v1/export` adds the full text of every report on a topic as
Markdown, fetched live from the newsroom and never stored. Every record links
back to the newsroom that filed it.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The serving path never writes. Creating the schema and syncing the source
    # registry are jobs for `python -m app.setup` and for ingestion, not for a
    # cold start — doing them here would put a write in front of every request
    # after an idle period, for work that has almost always already been done.
    conn = db.ServingConnection()
    app.state.conn = conn
    yield
    conn.close()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Nigerian News Router",
        version="0.1.0",
        description=DESCRIPTION,
        lifespan=lifespan,
        docs_url="/docs",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET"],
        allow_headers=["*"],
        # The export pages through these; a script on another origin needs them.
        expose_headers=["X-Next-Cursor", "X-Total", "X-Count", "X-Full-Text"],
    )
    for module in (meta, sources, articles, search, clusters, export, admin):
        app.include_router(module.router)

    @app.middleware("http")
    async def record_usage(request: Request, call_next):
        # Every search, download and call is recorded for the usage dashboard,
        # after the response is on its way. Nothing here can fail a request.
        started = time.monotonic()
        response = await call_next(request)
        try:
            row = usage.event(request, response.status_code, round((time.monotonic() - started) * 1000))
        except Exception:
            row = None
        if row and response.background is None:
            response.background = BackgroundTask(usage.write, row)
        return response

    # GET and HEAD: link-preview crawlers (LinkedIn among them) check with HEAD
    # first and give up on a 405.
    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    async def home():
        return FileResponse(STATIC_DIR / "index.html")

    @app.api_route("/admin", methods=["GET", "HEAD"], include_in_schema=False)
    async def admin_page():
        # The usage dashboard. The page is public; its data needs ADMIN_TOKEN.
        return FileResponse(STATIC_DIR / "admin.html")

    @app.api_route("/og.png", methods=["GET", "HEAD"], include_in_schema=False)
    async def share_image():
        # The picture link previews show (WhatsApp, X, LinkedIn).
        return FileResponse(STATIC_DIR / "og.png", headers={"Cache-Control": "public, max-age=86400"})

    return app


app = create_app()
