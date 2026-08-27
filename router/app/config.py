import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent

# Postgres. Vercel's Neon integration sets DATABASE_URL; locally it comes from
# .env.local. The pooled endpoint (host contains "-pooler") is the one to use
# from serverless — every cold start would otherwise open its own connection
# and exhaust the server long before traffic did.
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# The direct (non-pooled) endpoint, used for writes. Neon's integration sets
# both; falling back to the pooled URL keeps a single-URL setup working.
DATABASE_URL_DIRECT = os.environ.get("DATABASE_URL_UNPOOLED") or DATABASE_URL

# Credentials for a role holding SELECT and nothing else, over the pooled
# endpoint. The API reads through this, so a write from the serving path fails
# on permissions rather than on the honour system. Falls back to the ordinary
# URL when unset, which still works — it just stops enforcing the guarantee.
DATABASE_URL_READONLY = os.environ.get("DATABASE_URL_READONLY") or DATABASE_URL

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

# The gist writer. Two backends:
#   claude (default) — the production path; needs ANTHROPIC_API_KEY, and left
#     unset, clustering still runs and gists are skipped with a "not
#     configured" note.
#   ollama — a local server for testing, so summaries cost nothing while the
#     prompt and pipeline are being shaken out. Only reachable from the machine
#     running it, so the deployed cron never uses this.
# ROUTER_GIST_MODEL names the model within whichever backend is active.
GIST_BACKEND = os.environ.get("ROUTER_GIST_BACKEND", "claude")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OLLAMA_URL = os.environ.get("ROUTER_OLLAMA_URL", "http://localhost:11434")
_DEFAULT_GIST_MODELS = {"claude": "claude-opus-5", "ollama": "gemma4:e4b"}
GIST_MODEL = os.environ.get("ROUTER_GIST_MODEL") or _DEFAULT_GIST_MODELS.get(
    GIST_BACKEND, _DEFAULT_GIST_MODELS["claude"]
)
