"""Write the gist of each story cluster with Claude.

The model reads only what the API itself is allowed to serve: headline and
attribution always, dek and snippet only where that outlet's rights flags say
so. Bodies are never stored, so they can never leak in here. Each gist records
a hash of its inputs; a cluster whose coverage has not moved costs nothing on
the next run.
"""
from __future__ import annotations

import json
from typing import Any

import anthropic
from pydantic import BaseModel

from .config import ANTHROPIC_API_KEY, GIST_MODEL
from .normalize import content_hash, now_iso

# A gist is a few sentences plus one line per outlet — deliberately short.
MAX_TOKENS = 2048


class OutletNote(BaseModel):
    source_id: str
    note: str


class Gist(BaseModel):
    summary: str
    coverage: list[OutletNote]


SYSTEM = """You write the gist of a news story for a Nigerian news aggregator.

You are given what several newsrooms published about one story: each outlet's
headline and, where the outlet has licensed it, a short description. Write only
from that material. Never add facts, names, figures or background that are not
in it, and never guess at what an outlet meant.

Return:
- summary: two to four plain, neutral sentences saying what happened, drawn
  from all the outlets together. If their accounts disagree, say so and name
  which outlet says what.
- coverage: for each outlet, one note of at most 25 words on what its coverage
  adds or emphasises, using the outlet's source_id exactly as given. If an
  outlet contributes nothing beyond the shared facts, say what angle its
  headline takes.

Plain language throughout — no press-release phrasing, no editorialising."""


def input_hash(articles: list[Any]) -> str:
    """Moves when membership changes or any member's stored text changes."""
    return content_hash(*sorted(f"{a['id']}␟{a['content_hash']}" for a in articles))


def build_prompt(cluster: Any, articles: list[Any], sources: dict[str, Any]) -> str:
    """The user turn: one block per article, gated exactly like the serving path."""
    lines = [f"Story: {cluster['label']}", ""]
    for a in articles:
        src = sources[a["source_id"]]
        lines.append(f"outlet: {src['attribution_name']} (source_id: {src['id']})")
        lines.append(f"published: {a['published_at']}")
        lines.append(f"headline: {a['headline']}")
        if src["rights_dek"] and a["dek"]:
            lines.append(f"description: {a['dek']}")
        if src["rights_snippet"] and a["snippet"] and a["snippet"] != a["dek"]:
            lines.append(f"snippet: {a['snippet']}")
        if a["wire_source"]:
            lines.append(f"wire agency: {a['wire_source']}")
        lines.append("")
    return "\n".join(lines).strip()


def _call(client: anthropic.Anthropic, prompt: str) -> Gist:
    response = client.messages.parse(
        model=GIST_MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        output_format=Gist,
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("model declined to summarise this cluster")
    return response.parsed_output


def generate(conn, max_gists: int = 25) -> dict[str, Any]:
    """Write or refresh gists for clusters carried by at least two articles.

    `max_gists` caps actual model calls per run, not clusters examined, so a
    serverless invocation stays short; anything left over is picked up on the
    next run, newest stories first.
    """
    if not ANTHROPIC_API_KEY:
        return {"status": "not configured", "detail": "ANTHROPIC_API_KEY is unset; gists skipped."}

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    sources = {r["id"]: r for r in conn.execute("SELECT * FROM sources").fetchall()}
    clusters = conn.execute(
        "SELECT * FROM clusters WHERE size >= 2 ORDER BY last_published_at DESC"
    ).fetchall()

    stats: dict[str, Any] = {"status": "ok", "generated": 0, "unchanged": 0, "errors": []}
    for cluster in clusters:
        if stats["generated"] >= max_gists:
            break
        articles = conn.execute(
            "SELECT * FROM articles WHERE cluster_id = %s AND retracted = 0 ORDER BY published_at",
            (cluster["id"],),
        ).fetchall()
        if len(articles) < 2:
            continue

        fresh_hash = input_hash(articles)
        existing = conn.execute(
            "SELECT input_hash FROM cluster_gists WHERE cluster_id = %s", (cluster["id"],)
        ).fetchone()
        if existing and existing["input_hash"] == fresh_hash:
            stats["unchanged"] += 1
            continue

        prompt = build_prompt(cluster, articles, sources)
        try:
            gist = _call(client, prompt)
        except anthropic.RateLimitError:
            # The whole run is rate-limited, not just this cluster.
            stats["errors"].append({"cluster": cluster["id"], "error": "rate limited; run stopped"})
            break
        except (anthropic.APIStatusError, anthropic.APIConnectionError, RuntimeError) as exc:
            stats["errors"].append({"cluster": cluster["id"], "error": str(exc)})
            continue

        conn.execute(
            """INSERT INTO cluster_gists (cluster_id, summary, coverage, model, input_hash, generated_at)
               VALUES (%(cluster_id)s, %(summary)s, %(coverage)s, %(model)s, %(input_hash)s, %(generated_at)s)
               ON CONFLICT (cluster_id) DO UPDATE SET
                 summary = excluded.summary, coverage = excluded.coverage,
                 model = excluded.model, input_hash = excluded.input_hash,
                 generated_at = excluded.generated_at""",
            {
                "cluster_id": cluster["id"],
                "summary": gist.summary,
                "coverage": json.dumps([n.model_dump() for n in gist.coverage], ensure_ascii=False),
                "model": GIST_MODEL,
                "input_hash": fresh_hash,
                "generated_at": now_iso(),
            },
        )
        stats["generated"] += 1

    if stats["errors"]:
        stats["status"] = "degraded"
    return stats
