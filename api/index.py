"""Vercel entry point.

Vercel's Python runtime imports this module and serves whatever ASGI app it
finds as `app`. Everything real lives in ../app; this only makes that package
importable from inside the api/ directory.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import app  # noqa: E402

__all__ = ["app"]
