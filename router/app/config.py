import os
import shutil
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent


def _db_path() -> str:
    """Where the SQLite file lives.

    On a serverless host the deployment bundle is read-only, and SQLite needs
    to write even to serve reads — the WAL journal and the startup source sync
    both touch disk. So the bundled database is copied once per cold start to
    /tmp, the one writable place, and served from there. The copy is landed
    under a temporary name and renamed, so a half-written file is never opened
    if two workers start at the same moment.
    """
    if explicit := os.environ.get("ROUTER_DB"):
        return explicit

    bundled = ROOT_DIR / "router.db"
    if not os.environ.get("VERCEL"):
        return str(bundled)

    served = Path("/tmp/router.db")
    if not served.exists() and bundled.exists():
        staging = served.with_suffix(f".db.{os.getpid()}")
        shutil.copyfile(bundled, staging)
        staging.replace(served)
    return str(served)


DB_PATH = _db_path()
SOURCES_FILE = str(APP_DIR / "sources.yaml")
# Git-ignored overlay carrying where each agreement actually stands.
SOURCES_LOCAL_FILE = str(APP_DIR / "sources.local.yaml")

USER_AGENT = os.environ.get(
    "ROUTER_USER_AGENT",
    "NewsRouter/0.1 (+https://github.com/sforslime/news-router; contact ayoaopa3@gmail.com)",
)

# Requests per minute for an unauthenticated caller, and the default for new keys.
ANON_RATE_PER_MIN = int(os.environ.get("ROUTER_ANON_RATE", "30"))
DEFAULT_RATE_PER_MIN = int(os.environ.get("ROUTER_DEFAULT_RATE", "120"))

# Set to "1" to let unauthenticated callers read. Useful locally, off in prod.
ALLOW_ANON = os.environ.get("ROUTER_ALLOW_ANON", "1") == "1"

HTTP_TIMEOUT = float(os.environ.get("ROUTER_HTTP_TIMEOUT", "25"))
