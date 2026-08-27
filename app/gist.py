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
import httpx
from pydantic import BaseModel, ValidationError

from .config import (ANTHROPIC_API_KEY, GIST_BACKEND, GIST_MODEL, GROQ_API_KEY,
                     GROQ_URL, OLLAMA_URL)
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


TOPIC_SYSTEM = """You write the gist of recent news coverage on one topic, for a
Nigerian news aggregator.

You are given what several newsrooms published about the topic in the last few
days: each outlet's headline and, where the outlet has licensed it, a short
description. The items may span several distinct stories about the topic. Write
only from that material. Never add facts, names, figures or background that are
not in it, and never guess at what an outlet meant.

Write plain text, no markdown:
- First, one paragraph of two to five plain, neutral sentences on what has been
  published recently — group related items naturally, newest developments first.
  If accounts disagree, say so and name which outlet says what.
- Then a blank line, then one line per outlet in the form
  "Outlet name: what its coverage adds or emphasises", at most 20 words each.

No press-release phrasing, no editorialising."""


def input_hash(articles: list[Any]) -> str:
    """Moves when membership changes or any member's stored text changes."""
    return content_hash(*sorted(f"{a['id']}␟{a['content_hash']}" for a in articles))


def _article_block(a: Any, src: Any) -> list[str]:
    """One article as prompt lines, gated exactly like the serving path."""
    lines = [
        f"outlet: {src['attribution_name']} (source_id: {src['id']})",
        f"published: {a['published_at']}",
        f"headline: {a['headline']}",
    ]
    if src["rights_dek"] and a["dek"]:
        lines.append(f"description: {a['dek']}")
    if src["rights_snippet"] and a["snippet"] and a["snippet"] != a["dek"]:
        lines.append(f"snippet: {a['snippet']}")
    if a["wire_source"]:
        lines.append(f"wire agency: {a['wire_source']}")
    lines.append("")
    return lines


def build_prompt(cluster: Any, articles: list[Any], sources: dict[str, Any]) -> str:
    lines = [f"Story: {cluster['label']}", ""]
    for a in articles:
        lines.extend(_article_block(a, sources[a["source_id"]]))
    return "\n".join(lines).strip()


def build_topic_prompt(topic: str, articles: list[Any], sources: dict[str, Any]) -> str:
    lines = [f"Recent coverage of: {topic}", ""]
    for a in articles:
        lines.extend(_article_block(a, sources[a["source_id"]]))
    return "\n".join(lines).strip()


def _call_claude(client: anthropic.Anthropic, prompt: str) -> Gist:
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


def _raise_with_body(resp: httpx.Response) -> None:
    """A bare '404 Not Found' from Groq hides the actual reason (usually 'you
    do not have access to this model'). Surface the body's message instead."""
    if resp.status_code < 400:
        return
    if not resp.is_closed:
        resp.read()
    try:
        message = resp.json()["error"]["message"]
    except Exception:
        message = resp.text[:200]
    raise RuntimeError(f"HTTP {resp.status_code}: {message}")


def _call_groq(prompt: str) -> Gist:
    """Groq's chat-completions API over plain httpx. json_object mode plus the
    schema in the prompt; pydantic validates what actually came back."""
    resp = httpx.post(
        f"{GROQ_URL}/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
        json={
            "model": GIST_MODEL,
            "messages": [
                {"role": "system",
                 "content": SYSTEM + "\n\nReturn JSON matching exactly: "
                            '{"summary": "...", "coverage": [{"source_id": "...", "note": "..."}]}'},
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
            "max_tokens": MAX_TOKENS,
        },
        timeout=60.0,
    )
    _raise_with_body(resp)
    return Gist.model_validate_json(resp.json()["choices"][0]["message"]["content"])


def _call_ollama(prompt: str) -> Gist:
    """Same prompt, same schema-constrained JSON, against a local server.
    Generous timeout: a local model on laptop hardware takes what it takes."""
    resp = httpx.post(
        f"{OLLAMA_URL}/api/chat",
        json={
            "model": GIST_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": prompt},
            ],
            "format": Gist.model_json_schema(),
            "stream": False,
            "options": {"temperature": 0.2},
        },
        timeout=300.0,
    )
    resp.raise_for_status()
    return Gist.model_validate_json(resp.json()["message"]["content"])


def generate(conn, max_gists: int = 25) -> dict[str, Any]:
    """Write or refresh gists for clusters carried by at least two articles.

    `max_gists` caps actual model calls per run, not clusters examined, so a
    serverless invocation stays short; anything left over is picked up on the
    next run, newest stories first.
    """
    if GIST_BACKEND == "ollama":
        model_tag = f"ollama:{GIST_MODEL}"  # prefixed so a test model's gist is
        # told apart from a Claude one — and rewritten once the backend switches
        try:
            httpx.get(f"{OLLAMA_URL}/api/version", timeout=5.0)
        except httpx.HTTPError:
            # Not an error: the writer lives on a laptop that is allowed to be
            # closed. Recorded as such so the API can say so.
            return {"status": "offline", "backend": GIST_BACKEND, "model": model_tag,
                    "detail": f"The local model is offline — nothing answered at {OLLAMA_URL}."}
        call = _call_ollama
    elif GIST_BACKEND == "groq":
        model_tag = f"groq:{GIST_MODEL}"
        if not GROQ_API_KEY:
            return {"status": "not configured", "backend": GIST_BACKEND, "model": model_tag,
                    "detail": "GROQ_API_KEY is unset; gists skipped."}
        call = _call_groq
    else:
        model_tag = GIST_MODEL
        if not ANTHROPIC_API_KEY:
            return {"status": "not configured", "backend": GIST_BACKEND, "model": model_tag,
                    "detail": "ANTHROPIC_API_KEY is unset; gists skipped."}
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        call = lambda prompt: _call_claude(client, prompt)
    sources = {r["id"]: r for r in conn.execute("SELECT * FROM sources").fetchall()}
    clusters = conn.execute(
        "SELECT * FROM clusters WHERE size >= 2 ORDER BY last_published_at DESC"
    ).fetchall()

    stats: dict[str, Any] = {"status": "ok", "backend": GIST_BACKEND, "model": model_tag,
                             "generated": 0, "unchanged": 0, "errors": []}
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
            "SELECT input_hash, model FROM cluster_gists WHERE cluster_id = %s", (cluster["id"],)
        ).fetchone()
        # A gist is stale when the coverage moved — or when a different model
        # wrote it, so switching backend replaces test output instead of
        # serving it forever.
        if existing and existing["input_hash"] == fresh_hash and existing["model"] == model_tag:
            stats["unchanged"] += 1
            continue

        prompt = build_prompt(cluster, articles, sources)
        try:
            gist = call(prompt)
        except anthropic.RateLimitError:
            # The whole run is rate-limited, not just this cluster.
            stats["errors"].append({"cluster": cluster["id"], "error": "rate limited; run stopped"})
            break
        except (anthropic.APIStatusError, anthropic.APIConnectionError,
                httpx.HTTPError, ValidationError, KeyError, RuntimeError) as exc:
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
                "model": model_tag,
                "input_hash": fresh_hash,
                "generated_at": now_iso(),
            },
        )
        stats["generated"] += 1

    if stats["errors"]:
        stats["status"] = "degraded"
    return stats


# ---------------------------------------------------------------------------
# Streaming, for the on-demand topic gist. Same backend switch as generate(),
# but the writer hands back text deltas instead of a validated Gist — a topic
# summary is prose that streams into the page as it is written.


def _stream_groq(system: str, prompt: str):
    with httpx.stream(
        "POST",
        f"{GROQ_URL}/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
        json={
            "model": GIST_MODEL,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": prompt}],
            "stream": True,
            "temperature": 0.2,
            "max_tokens": MAX_TOKENS,
        },
        timeout=60.0,
    ) as resp:
        _raise_with_body(resp)
        for line in resp.iter_lines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            chunk = json.loads(line[len("data: "):])
            delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
            if delta.get("content"):
                yield delta["content"]


def _stream_ollama(system: str, prompt: str):
    with httpx.stream(
        "POST",
        f"{OLLAMA_URL}/api/chat",
        json={
            "model": GIST_MODEL,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": prompt}],
            "stream": True,
            "options": {"temperature": 0.2},
        },
        timeout=300.0,
    ) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line:
                continue
            chunk = json.loads(line)
            content = (chunk.get("message") or {}).get("content")
            if content:
                yield content


def _stream_claude(system: str, prompt: str):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    with client.messages.stream(
        model=GIST_MODEL,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        yield from stream.text_stream


def stream_writer():
    """(stream_fn, model_tag), or a status dict when no writer is available —
    the same outcomes and wording generate() reports."""
    if GIST_BACKEND == "ollama":
        model_tag = f"ollama:{GIST_MODEL}"
        try:
            httpx.get(f"{OLLAMA_URL}/api/version", timeout=5.0)
        except httpx.HTTPError:
            return {"status": "offline", "backend": GIST_BACKEND, "model": model_tag,
                    "detail": f"The local model is offline — nothing answered at {OLLAMA_URL}."}
        return _stream_ollama, model_tag
    if GIST_BACKEND == "groq":
        model_tag = f"groq:{GIST_MODEL}"
        if not GROQ_API_KEY:
            return {"status": "not configured", "backend": GIST_BACKEND, "model": model_tag,
                    "detail": "GROQ_API_KEY is unset; gists skipped."}
        return _stream_groq, model_tag
    if not ANTHROPIC_API_KEY:
        return {"status": "not configured", "backend": GIST_BACKEND, "model": GIST_MODEL,
                "detail": "ANTHROPIC_API_KEY is unset; gists skipped."}
    return _stream_claude, GIST_MODEL
