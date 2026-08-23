from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from . import db
from .config import APP_DIR
from .routes import articles, clusters, meta, search, sources

STATIC_DIR = APP_DIR / "static"

DESCRIPTION = """
One API across Nigerian newsrooms.

Returns **metadata only** — headline, dek, byline, timestamp, canonical URL,
snippet and thumbnail — never article bodies, at any ingestion tier. Every
record carries the rights its publisher granted, and fields outside that grant
come back as `null` rather than being silently omitted.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = db.connect()
    db.init_db(conn)
    db.sync_sources(conn)
    # SQLite connections are single-threaded by default; FastAPI's threadpool
    # can hand requests to different threads, and reads here are serialised.
    conn.execute("PRAGMA query_only = ON")
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
    )
    for module in (meta, sources, articles, search, clusters):
        app.include_router(module.router)

    @app.get("/", include_in_schema=False)
    async def home():
        return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()
