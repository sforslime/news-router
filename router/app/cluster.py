"""Group articles from different outlets into story clusters.

Runs incrementally: only articles without a cluster_id are ever assigned, so a
re-run never reshuffles what earlier runs decided. Matching uses the metadata
the router stores — headline words, publisher entity tags, wire markers —
because bodies are never persisted. That buys precision more than recall: two
outlets writing the same event under very different headlines will sometimes
stay apart, which is the honest failure mode for a grouping the site presents
as "the same story".
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .normalize import content_hash, now_iso

# How far back matching looks. Nigerian dailies re-report a running story for a
# couple of days; beyond that a headline-word match is usually a new development
# that deserves its own cluster.
WINDOW_HOURS = 72

# Two articles are the same story when they share at least MIN_SHARED_WORDS
# content words AND that overlap covers at least MIN_OVERLAP of the smaller
# word set. Both are needed: the count alone lets two long headlines drift
# together on common words, the ratio alone lets two-word headlines match
# anything. Picked by hand against the August 2026 index; loosen with care.
MIN_SHARED_WORDS = 3
MIN_OVERLAP = 0.5

# Sharing two full publisher-curated entity tags ("Bola Tinubu" + "ICPC") is a
# match on its own — tags are deliberate editorial descriptions, not prose.
MIN_SHARED_ENTITIES = 2

# Wire copy runs near-identically everywhere, so same agency + half the words
# in common is enough.
WIRE_MIN_JACCARD = 0.5

_WORD_RE = re.compile(r"[a-z0-9']+")

# Words too common in Nigerian news copy to signal a shared story.
_STOPWORDS = {
    "a", "about", "after", "against", "amid", "an", "and", "as", "at", "be",
    "before", "between", "breaking", "but", "by", "day", "days", "for", "from",
    "govt", "has", "have", "he", "her", "his", "how", "in", "into", "is", "it",
    "its", "man", "men", "more", "new", "news", "nigeria", "nigerian",
    "nigerians", "not", "of", "off", "on", "onto", "or", "our", "over", "reps",
    "said", "say", "says", "she", "state", "than", "that", "the", "their",
    "they", "this", "to", "top", "under", "up", "us", "was", "we", "what",
    "when", "where", "who", "why", "will", "with", "year", "years", "you",
}


@dataclass
class _Story:
    id: str
    source_id: str
    published_at: str
    cluster_id: str | None
    headline: str
    entities: frozenset[str]
    words: frozenset[str]
    wire_source: str | None


def _words(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(text.lower()) if len(w) > 1 and w not in _STOPWORDS}


def _story(row: Any) -> _Story:
    # Some feeds tag every article with branding filler — Peoples Gazette ships
    # ["news", "Nigeria", "Nigerian news", ...] on all of them — and two pieces
    # of filler must not read as two shared entities. A tag only counts as an
    # entity if it still says something once stopwords are gone.
    entities = [
        e.strip().lower()
        for e in json.loads(row["entities"] or "[]")
        if _words(e)
    ]
    words = _words(row["headline"])
    for entity in entities:
        words |= _words(entity)
    return _Story(
        id=row["id"],
        source_id=row["source_id"],
        published_at=row["published_at"],
        cluster_id=row["cluster_id"],
        headline=row["headline"],
        entities=frozenset(e for e in entities if e),
        words=frozenset(words),
        wire_source=row["wire_source"],
    )


def shared_words(a: _Story, b: _Story) -> int:
    return len(a.words & b.words)


def same_story(a: _Story, b: _Story) -> bool:
    """The pinned matching rule. Pure, so tests can hold it still."""
    if not a.words or not b.words:
        return False
    inter = a.words & b.words
    if a.wire_source and a.wire_source == b.wire_source:
        jaccard = len(inter) / len(a.words | b.words)
        if jaccard >= WIRE_MIN_JACCARD:
            return True
    if len(a.entities & b.entities) >= MIN_SHARED_ENTITIES:
        return True
    return (
        len(inter) >= MIN_SHARED_WORDS
        and len(inter) / min(len(a.words), len(b.words)) >= MIN_OVERLAP
    )


def run(conn) -> dict[str, int]:
    """Assign unclustered window articles; returns counts for the digest report."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)).isoformat().replace("+00:00", "Z")
    rows = conn.execute(
        """SELECT id, source_id, headline, entities, wire_source, published_at, cluster_id
           FROM articles
           WHERE published_at >= %s AND sponsored = 0 AND retracted = 0
           ORDER BY published_at""",
        (cutoff,),
    ).fetchall()
    stories = [_story(r) for r in rows]

    assigned = 0
    touched: set[str] = set()
    for story in [s for s in stories if s.cluster_id is None]:
        if story.cluster_id is not None:  # picked up as someone's first match below
            continue
        best: _Story | None = None
        for other in stories:
            if other is story or other.source_id == story.source_id:
                continue
            if not same_story(story, other):
                continue
            # Prefer the strongest word overlap; on a tie, an already-clustered
            # match, so chains converge on one cluster instead of founding two.
            if (
                best is None
                or shared_words(story, other) > shared_words(story, best)
                or (
                    shared_words(story, other) == shared_words(story, best)
                    and other.cluster_id and not best.cluster_id
                )
            ):
                best = other

        if best is None:
            continue

        if best.cluster_id is None:
            lead, later = sorted((story, best), key=lambda s: s.published_at)
            cluster_id = "c-" + content_hash(lead.id, later.id)[:12]
            conn.execute(
                """INSERT INTO clusters (id, label, lead_article_id, size,
                                         first_published_at, last_published_at, created_at, updated_at)
                   VALUES (%s, %s, %s, 0, %s, %s, %s, %s)
                   ON CONFLICT (id) DO NOTHING""",
                (cluster_id, lead.headline, lead.id, lead.published_at,
                 later.published_at, now_iso(), now_iso()),
            )
            conn.execute("UPDATE articles SET cluster_id = %s WHERE id = %s", (cluster_id, best.id))
            best.cluster_id = cluster_id
            assigned += 1
        else:
            cluster_id = best.cluster_id

        conn.execute("UPDATE articles SET cluster_id = %s WHERE id = %s", (cluster_id, story.id))
        story.cluster_id = cluster_id
        assigned += 1
        touched.add(cluster_id)

    for cluster_id in touched:
        _refresh(conn, cluster_id)

    return {"scanned": len(stories), "assigned": assigned, "clusters_touched": len(touched)}


def _refresh(conn, cluster_id: str) -> None:
    conn.execute(
        """UPDATE clusters SET
             size = agg.n,
             first_published_at = agg.first,
             last_published_at = agg.last,
             updated_at = %(now)s
           FROM (SELECT COUNT(*) AS n, MIN(published_at) AS first, MAX(published_at) AS last
                 FROM articles WHERE cluster_id = %(cid)s) AS agg
           WHERE clusters.id = %(cid)s""",
        {"cid": cluster_id, "now": now_iso()},
    )
