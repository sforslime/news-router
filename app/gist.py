"""Write the gist of each story cluster with Claude.

The model reads the headline and attribution, plus the dek and the report's
opening paragraphs where the outlet provides them (kept three days, never
served). Each gist records
a hash of its inputs; a cluster whose coverage has not moved costs nothing on
the next run.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import anthropic
import httpx
from pydantic import BaseModel, ValidationError

from .config import (ANTHROPIC_API_KEY, GIST_BACKEND, GIST_MODEL, GROQ_API_KEY,
                     GROQ_SEARCH_API_KEY, GROQ_URL, OLLAMA_URL)
from .normalize import content_hash, now_iso

# A gist is a few sentences plus one line per outlet — deliberately short.
# The output cap also bounds the model's own reasoning, so it stays modest:
# Groq's free plan counts every token against a per-minute and per-day limit.
MAX_TOKENS = 1024

# Groq's free plan (openai/gpt-oss-120b): about 30 requests and 8,000 tokens a
# minute, 200,000 tokens a day, per account. A gist is ~3k tokens, so calls
# are spaced to stay under the per-minute cap, and a run stops starting new
# calls in time to finish inside Vercel's 300s function limit.
GROQ_CALL_GAP = 25.0
RUN_BUDGET_S = 230.0
SHORT_WAIT_S = 20.0           # a 429 asking for at most this is waited out once
PROMPT_MAX_ARTICLES = 6       # past this a big story costs more and adds little
RECENT_HOURS = 48             # only stories still moving get a gist


class RateLimited(RuntimeError):
    """The provider refused for volume, not for this story. Carries how long it
    asked us to wait."""

    def __init__(self, message: str, retry_after: float):
        super().__init__(message)
        self.retry_after = retry_after


class OutletNote(BaseModel):
    source_id: str
    note: str


class Gist(BaseModel):
    summary: str
    coverage: list[OutletNote]


SYSTEM = """You write the gist of a news story for a Nigerian news aggregator.

You are given what several newsrooms published about one story: each outlet's
headline and, where provided, a short description and the report's opening
paragraphs. Write only from that material. Never add facts, names, figures or background that are not
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

You are given a MAIN STORY: what several newsrooms published about it, each
with a headline and, where provided, a short description and the report's
opening paragraphs. You may also be given OTHER MATCHING REPORTS: headlines of
separate stories that matched the same search. Write only from that material.
Never add facts, names, figures or background that are not in it, and never
guess at what an outlet meant.

Write plain text, no markdown:
- First, one paragraph of two to four plain, neutral sentences on the main
  story only: what happened, newest developments first. If accounts disagree,
  say so and name which outlet says what.
- If other matching reports were given, add one sentence beginning
  "Also in the news:" that names them briefly. Do not merge them into the main
  story.
- Then a blank line, then one line per outlet that covered the main story, in
  the form "Outlet name: what its coverage adds or emphasises", at most 20
  words each.

No press-release phrasing, no editorialising."""


# Bumped when what a prompt carries changes, so existing gists are rewritten
# with the richer input. v2: opening paragraphs instead of the snippet.
PROMPT_VERSION = "v2"


def input_hash(articles: list[Any]) -> str:
    """Moves when membership changes, any member's stored text changes, or the
    prompt format does."""
    return content_hash(PROMPT_VERSION, *sorted(f"{a['id']}␟{a['content_hash']}" for a in articles))


def _article_block(a: Any, src: Any) -> list[str]:
    """One article as prompt lines: the fields the API serves, plus the opening
    paragraphs where kept (never served; see normalize.LEAD_CHARS)."""
    lines = [
        f"outlet: {src['attribution_name']} (source_id: {src['id']})",
        f"published: {a['published_at']}",
        f"headline: {a['headline']}",
    ]
    lead = a.get("lead_text")
    # Many outlets' excerpt is just the article's first lines; when the opening
    # already starts with it, sending both only doubles the tokens.
    if a["dek"] and not (lead and lead.startswith(a["dek"].rstrip(" .…[]")[:100])):
        lines.append(f"description: {a['dek']}")
    if lead:
        # The snippet is the first 320 characters of this same text.
        lines.append(f"opening: {a['lead_text']}")
    elif a["snippet"] and a["snippet"] != a["dek"]:
        lines.append(f"snippet: {a['snippet']}")
    if a["wire_source"]:
        lines.append(f"wire agency: {a['wire_source']}")
    lines.append("")
    return lines


def pick_articles(articles: list[Any], n: int = PROMPT_MAX_ARTICLES) -> list[Any]:
    """At most n articles, one per outlet before any outlet gets a second, newest
    first; returned oldest first so the prompt reads in order."""
    newest = sorted(articles, key=lambda a: a["published_at"] or "", reverse=True)
    picked, outlets = [], set()
    for a in newest:
        if a["source_id"] not in outlets:
            picked.append(a)
            outlets.add(a["source_id"])
    picked += [a for a in newest if a not in picked]
    return sorted(picked[:n], key=lambda a: a["published_at"] or "")


def build_prompt(cluster: Any, articles: list[Any], sources: dict[str, Any]) -> str:
    lines = [f"Story: {cluster['label']}", ""]
    for a in pick_articles(articles):
        lines.extend(_article_block(a, sources[a["source_id"]]))
    return "\n".join(lines).strip()


def build_topic_prompt(topic: str, articles: list[Any], sources: dict[str, Any],
                       related: list[Any] = ()) -> str:
    """The main story in full, then other matching reports as headlines only."""
    lines = [f"Recent coverage of: {topic}", "", "MAIN STORY", ""]
    for a in articles:
        lines.extend(_article_block(a, sources[a["source_id"]]))
    if related:
        lines += ["OTHER MATCHING REPORTS", ""]
        lines += [f"{sources[a['source_id']]['attribution_name']}: {a['headline']}" for a in related]
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
    if resp.status_code == 429:
        try:
            wait = float(resp.headers.get("retry-after") or 60)
        except ValueError:
            wait = 60.0
        raise RateLimited(f"HTTP 429: {message}", retry_after=wait)
    raise RuntimeError(f"HTTP {resp.status_code}: {message}")


def _groq_reasoning() -> dict[str, str]:
    """gpt-oss reasons before answering, at 'medium' by default; 'low' is plenty
    for a summary and spends far fewer tokens. Other Groq models reject the field."""
    return {"reasoning_effort": "low"} if "gpt-oss" in GIST_MODEL else {}


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
            **_groq_reasoning(),
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


_clock = time.monotonic
_sleep = time.sleep


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
    # Stories still moving, the most widely covered first: those are the ones
    # the front page leads with, and a run may only get through a dozen or so.
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=RECENT_HOURS)).isoformat().replace("+00:00", "Z")
    clusters = conn.execute(
        """SELECT * FROM clusters WHERE size >= 2 AND last_published_at >= %s
           ORDER BY size DESC, last_published_at DESC""",
        (cutoff,),
    ).fetchall()

    paced = GIST_BACKEND == "groq"
    started = _clock()
    stats: dict[str, Any] = {"status": "ok", "backend": GIST_BACKEND, "model": model_tag,
                             "generated": 0, "unchanged": 0, "errors": []}
    for position, cluster in enumerate(clusters):
        if stats["generated"] >= max_gists:
            break
        if _clock() - started > RUN_BUDGET_S:
            stats["detail"] = f"Run time used up; {len(clusters) - position} stories left for the next run."
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
        if paced and stats["generated"]:
            _sleep(GROQ_CALL_GAP)
        try:
            try:
                gist = call(prompt)
            except RateLimited as exc:
                # A short wait is asked for when only the per-minute cap is hit:
                # wait it out once. A long one means the daily cap — stop.
                if exc.retry_after > SHORT_WAIT_S or _clock() - started + exc.retry_after > RUN_BUDGET_S:
                    raise
                _sleep(exc.retry_after)
                gist = call(prompt)
        except RateLimited as exc:
            # The whole account is limited, not just this story: trying the
            # rest would only burn the daily request allowance.
            left = len(clusters) - position
            stats["errors"].append({"cluster": cluster["id"], "error": str(exc)})
            stats["detail"] = f"Groq's free-plan limit reached; {left} stories left for the next run."
            break
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
        headers={"Authorization": f"Bearer {GROQ_SEARCH_API_KEY}"},
        json={
            "model": GIST_MODEL,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": prompt}],
            "stream": True,
            "temperature": 0.2,
            "max_tokens": MAX_TOKENS,
            **_groq_reasoning(),
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
        if not GROQ_SEARCH_API_KEY:
            return {"status": "not configured", "backend": GIST_BACKEND, "model": model_tag,
                    "detail": "GROQ_API_KEY is unset; gists skipped."}
        return _stream_groq, model_tag
    if not ANTHROPIC_API_KEY:
        return {"status": "not configured", "backend": GIST_BACKEND, "model": GIST_MODEL,
                "detail": "ANTHROPIC_API_KEY is unset; gists skipped."}
    return _stream_claude, GIST_MODEL
