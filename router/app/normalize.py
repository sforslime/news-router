"""Map publisher payloads onto the router's unified schema.

The router stores metadata only. Article bodies are read transiently — to compute
a change-detection hash and to spot wire copy — and are never persisted.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import datetime, timezone
from typing import Any

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# Script and style bodies survive plain tag-stripping as text. Beyond being
# noise, some themes inject ad containers whose element ids are regenerated on
# every request — hashing that makes an article look edited on every fetch.
_SCRIPT_RE = re.compile(r"<(script|style|noscript)\b[^>]*>.*?</\1>", re.I | re.S)

# Wire markers, checked against the body. A story that is agency copy is very
# likely to appear near-identically at several outlets, which is the cheapest
# clustering signal available.
_WIRE_PATTERNS = [
    ("NAN", re.compile(r"\(NAN\)|News Agency of Nigeria", re.I)),
    ("Reuters", re.compile(r"\bReuters\b", re.I)),
    ("AFP", re.compile(r"\bAgence France-Presse\b|\(AFP\)", re.I)),
    ("AP", re.compile(r"\bAssociated Press\b", re.I)),
    ("dpa", re.compile(r"\bdpa\b")),
]

# Deliberately conservative: only flag a non-English language on several hits,
# and default to English otherwise. A placeholder for a real classifier.
# Sponsored and syndicated-PR markers. These should not be indexed as reporting
# and must never be clustered with it.
_SPONSORED_SECTIONS = {"promoted stories", "promoted", "sponsored", "advertorial",
                       "press release", "press releases", "partner content", "brand feature"}
_SPONSORED_BYLINES = {"press release", "sponsored", "advertorial", "brand desk", "partner"}

_LANG_MARKERS = {
    "ha": {"kuma", "wanda", "shugaban", "sun", "gwamnati", "jihar", "mutane", "cikin", "yayin"},
    "yo": {"awon", "ati", "ijoba", "won", "nipa", "orile", "ede", "eyi", "ohun"},
    "pcm": {"dey", "wey", "abeg", "sabi", "wetin", "una", "don", "make", "no be"},
}


def strip_html(value: str | None) -> str:
    if not value:
        return ""
    text = _SCRIPT_RE.sub(" ", value)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def collapse_duplicate(text: str) -> str:
    """Collapse text that is one passage repeated twice.

    Several Nigerian WordPress installs run share-button plugins that filter
    `get_the_excerpt` and emit the excerpt twice in a row. Left alone it doubles
    every dek and poisons text similarity for clustering.
    """
    s = text.strip()
    if len(s) < 40:
        return s
    for cut in (len(s) // 2, (len(s) + 1) // 2):
        head, tail = s[:cut].strip(), s[cut:].strip()
        if head and head == tail:
            return head
    return s


def truncate(text: str, limit: int) -> str:
    """Cut on a word boundary so snippets do not end mid-word."""
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:.—-")
    return cut + "…"


def to_utc_iso(value: str | None, assume_utc: bool = True) -> str | None:
    """WordPress `date_gmt` has no offset suffix but is UTC. `date` is site-local."""
    if not value:
        return None
    raw = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None and assume_utc:
        dt = dt.replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        return dt.isoformat()
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def detect_wire(body: str) -> str | None:
    for name, pattern in _WIRE_PATTERNS:
        if pattern.search(body):
            return name
    return None


def detect_language(text: str) -> str:
    words = set(re.findall(r"[a-z']+", text.lower()))
    best, best_hits = "en", 0
    for lang, markers in _LANG_MARKERS.items():
        hits = len(words & markers)
        if hits > best_hits:
            best, best_hits = lang, hits
    return best if best_hits >= 3 else "en"


def content_hash(*parts: str) -> str:
    joined = "␟".join(_WS_RE.sub(" ", p or "").strip() for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:32]


# Tags that drive page furniture rather than describe the story. Every outlet
# invents its own, so this stays a denylist plus a couple of shape rules.
_LAYOUT_TAGS = {
    "billboard article", "featured", "slider", "top story", "main story",
    "breaking", "breaking news", "latest", "trending", "must read", "editor's pick",
    "editors pick", "exclusive", "promoted", "sponsored", "spotlight", "carousel",
}


def _is_layout_tag(name: str) -> bool:
    lowered = name.strip().lower()
    if lowered.startswith("#"):          # Ripples uses '#featured'
        return True
    if re.fullmatch(r"[a-z ]*headline\s*\d*", lowered):   # Premium Times uses 'Headline1'
        return True
    return lowered in _LAYOUT_TAGS


def _wp_terms(post: dict[str, Any]) -> tuple[str | None, list[str]]:
    """Return (section, entities) from an _embed'ed WordPress post.

    Nigerian outlets tag posts with entity names — 'President Bola Tinubu', 'ICPC'.
    That is a free, publisher-curated entity signal worth keeping for clustering.
    """
    groups = (post.get("_embedded") or {}).get("wp:term") or []
    section, entities = None, []
    for group in groups:
        for term in group or []:
            name = strip_html(term.get("name"))
            if not name:
                continue
            taxonomy = term.get("taxonomy")
            if taxonomy == "category" and section is None:
                section = name
            elif taxonomy == "post_tag":
                entities.append(name)
    entities = [e for e in entities if not _is_layout_tag(e)]
    return section, entities


def is_sponsored(section: str | None, byline: str | None) -> bool:
    if section and section.strip().lower() in _SPONSORED_SECTIONS:
        return True
    if byline and byline.strip().lower() in _SPONSORED_BYLINES:
        return True
    return False


def _wp_byline(post: dict[str, Any]) -> str | None:
    coauthors = post.get("coauthors")
    if isinstance(coauthors, list) and coauthors:
        names = [strip_html(c.get("display_name") or c.get("name") or "") for c in coauthors if isinstance(c, dict)]
        names = [n for n in names if n]
        if names:
            return ", ".join(names)
    authors = (post.get("_embedded") or {}).get("author") or []
    names = [strip_html(a.get("name", "")) for a in authors if isinstance(a, dict)]
    names = [n for n in names if n]
    return ", ".join(names) or None


def _wp_image(post: dict[str, Any]) -> str | None:
    media = (post.get("_embedded") or {}).get("wp:featuredmedia") or []
    for item in media:
        if isinstance(item, dict) and item.get("source_url"):
            return item["source_url"]
    return post.get("jetpack_featured_media_url") or None


def _wp_canonical(post: dict[str, Any]) -> str:
    yoast = post.get("yoast_head_json") or {}
    return yoast.get("canonical") or post.get("link") or ""


def normalize_wordpress(post: dict[str, Any], source_id: str) -> dict[str, Any]:
    """WordPress REST post -> unified router record."""
    headline = strip_html((post.get("title") or {}).get("rendered"))
    dek = collapse_duplicate(strip_html((post.get("excerpt") or {}).get("rendered")))
    body = strip_html((post.get("content") or {}).get("rendered"))

    section, entities = _wp_terms(post)
    byline = _wp_byline(post)
    published = to_utc_iso(post.get("date_gmt")) or to_utc_iso(post.get("date"))
    snippet = truncate(collapse_duplicate(body) or dek, 320)

    return {
        "id": f"{source_id}:{post.get('id')}",
        "source_id": source_id,
        "source_article_id": str(post.get("id")),
        "headline": headline,
        "dek": truncate(dek, 400) or None,
        "byline": byline,
        "published_at": published,
        "published_at_reported": post.get("date"),
        "updated_at": to_utc_iso(post.get("modified_gmt")),
        "first_seen_at": now_iso(),
        "canonical_url": _wp_canonical(post),
        "section": section,
        "snippet": snippet or None,
        "image": _wp_image(post),
        "language": detect_language(f"{headline} {dek}"),
        # Hash covers the body so a silent edit is caught even when the publisher
        # leaves `modified` untouched.
        "content_hash": content_hash(headline, dek, body),
        "wire_source": detect_wire(body),
        "paywalled": 0,
        "sponsored": int(is_sponsored(section, byline)),
        "entities": json.dumps(entities, ensure_ascii=False),
        "raw_status": post.get("status"),
    }
