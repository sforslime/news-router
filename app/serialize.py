"""Turn stored rows into API responses.

Every newsroom is served the same way: whatever front-of-story fields it
published. Article bodies are never stored, so they can never be served.
"""
from __future__ import annotations

import json
from typing import Any


def article_out(row: Any, source: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "source": {
            "id": source["id"],
            "name": source["name"],
            "attribution": source["attribution_name"],
        },
        "headline": row["headline"],
        "dek": row["dek"],
        "byline": row["byline"],
        "published_at": row["published_at"],
        "updated_at": row["updated_at"],
        "first_seen_at": row["first_seen_at"],
        "canonical_url": row["canonical_url"],
        "section": row["section"],
        "snippet": row["snippet"],
        "image": row["image"],
        "language": row["language"],
        "wire_source": row["wire_source"],
        "paywalled": bool(row["paywalled"]),
        "sponsored": bool(row["sponsored"]),
        "entities": json.loads(row["entities"] or "[]"),
        "revision": row["revision"],
        "retracted": bool(row["retracted"]),
        "retraction_note": row["retraction_note"],
        "cluster_id": row["cluster_id"],
    }


def source_out(row: Any) -> dict[str, Any]:
    """Public shape of a source."""
    return {
        "id": row["id"],
        "name": row["name"],
        "homepage": row["homepage"],
        "attribution": row["attribution_name"],
        "ingestion": row["adapter"],
        "enabled": bool(row["enabled"]),
        "last_ingest_at": row["last_ingest_at"],
        "last_error": row["last_error"],
    }
