import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent

# Postgres. Vercel's Neon integration sets DATABASE_URL; locally it comes from
# .env.local. The pooled endpoint (host contains "-pooler") is the one to use
# from serverless — every cold start would otherwise open its own connection
# and exhaust the server long before traffic did.
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# A separate database for the test suite, so tests can drop and rebuild tables
# without touching real data. Tests that need storage skip when this is unset.
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")

# Shared secret Vercel Cron presents when it calls the ingest endpoint.
CRON_SECRET = os.environ.get("CRON_SECRET", "")

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
