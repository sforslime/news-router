"""Turn stored rows into API responses, enforcing per-source licensing.

A record is only ever emitted with the fields its source's agreement permits.
Because tiers are mixed in a single response, rights have to be applied per
record, not per request.
"""
from __future__ import annotations

import json
from typing import Any


def article_out(row: Any, source: Any, include_rights: bool = True) -> dict[str, Any]:
    allow_dek = bool(source["rights_dek"])
    allow_snippet = bool(source["rights_snippet"])
    allow_image = bool(source["rights_image"])

    out = {
        "id": row["id"],
        "source": {
            "id": source["id"],
            "name": source["name"],
            "attribution": source["attribution_name"],
            "tier": source["tier"],
        },
        "headline": row["headline"],
        "dek": row["dek"] if allow_dek else None,
        "byline": row["byline"],
        "published_at": row["published_at"],
        "updated_at": row["updated_at"],
        "first_seen_at": row["first_seen_at"],
        "canonical_url": row["canonical_url"],
        "section": row["section"],
        "snippet": row["snippet"] if allow_snippet else None,
        "image": row["image"] if allow_image else None,
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
    if include_rights:
        # Explicit, so a consumer can tell "absent because not licensed" from
        # "absent because the publisher never supplied it".
        out["rights"] = {
            "dek": allow_dek,
            "snippet": allow_snippet,
            "image": allow_image,
            "body": False,  # never licensed, at any tier
        }
    return out


def source_out(row: Any) -> dict[str, Any]:
    """Public shape of a source.

    Where an agreement stands is tracked internally but not published — it is
    between the router and the newsroom, and a half-finished negotiation is not
    the caller's business. What the caller needs is the rights block below,
    which says what may actually be used.
    """
    return {
        "id": row["id"],
        "name": row["name"],
        "homepage": row["homepage"],
        "attribution": row["attribution_name"],
        "tier": row["tier"],
        "ingestion": row["adapter"],
        "enabled": bool(row["enabled"]),
        "rights": {
            "dek": bool(row["rights_dek"]),
            "snippet": bool(row["rights_snippet"]),
            "image": bool(row["rights_image"]),
            "body": False,
        },
        "last_ingest_at": row["last_ingest_at"],
        "last_error": row["last_error"],
    }
