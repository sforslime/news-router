# Nigerian News Router

Built by SAYOL labs.

**Live:** https://news-router.vercel.app · [API docs](https://news-router.vercel.app/docs)

One read API across Nigerian newsrooms. It reads what each newsroom already
publishes openly — its WordPress REST endpoint, or its RSS feed where that is
blocked — and normalises both into a single schema, so a consumer never learns
which a story arrived through. Twenty-three outlets are indexed today, and the same story is grouped across them. Each grouped story
gets a short gist — what happened, then a line on what each outlet's coverage
adds — written only from what the outlets published.

**Metadata only.** Headline, dek, byline, timestamps, canonical URL, section,
snippet and thumbnail. Article bodies are read transiently during ingestion — to
hash for change detection and to spot wire copy — and are never stored or served.

## Run it

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements-dev.txt

export DATABASE_URL='postgresql://…'          # see Storage below
./.venv/bin/python -m app.setup               # create tables, load sources.yaml
./.venv/bin/python -m app.ingest --limit 200       # every enabled newsroom
./.venv/bin/python -m app.digest                  # cluster, then write the gists
./.venv/bin/python -m uvicorn app.main:app --port 8099
```

Then open `http://localhost:8099/`.

The digest step needs a gist writer. Groq is the default and what the deployed
site uses: set `GROQ_API_KEY` (hosted, free tier; default model
`openai/gpt-oss-120b`). The alternatives are `ROUTER_GIST_BACKEND=claude` with
`ANTHROPIC_API_KEY`, or `=ollama` for a local model, fully offline.
`ROUTER_GIST_MODEL` picks a different model within whichever backend is active.
Without a key, clustering still runs and gists are skipped with a note saying
why; a local model that isn't running is reported as offline. Either way,
`/v1/clusters` then says so.

## Layout

```
app/
  main.py           FastAPI app; the serving path never writes
  routes/           one module per endpoint group
  adapters/         one per ingestion mechanism — wordpress, rss
  normalize.py      timestamps, doubled excerpts, HTML stripping, wire detection
  cluster.py        group the same story across outlets
  gist.py           write a story's gist; Groq by default, Claude or a local Ollama as alternatives
  serialize.py      stored rows to API responses
  auth.py           API keys and the per-instance rate limiter
  config.py         settings from the environment, including the gist-writer switch
  db.py schema.sql  Postgres, hosted on Neon
  sources.yaml      the roster — names, endpoints, which are switched on
  static/index.html the front page, one file, no build step
  ingest.py digest.py setup.py keys.py    the CLIs
  migrate_from_sqlite.py                  one-off move from the old SQLite file
api/index.py        Vercel entry point
tests/
```

## The front page

`/` serves a single static page from `app/static/index.html` — no build step, no
second deployment. It is a working demonstration rather than a product surface:
live search across every indexed newsroom, the roster of newsrooms read today, and a developer view whose endpoints run against the live index
and print the real response with timing and payload size.

On arrival the page shows the gist the morning digest already wrote for the
day's most-covered story (the biggest group from the last 24 hours), so opening
the page is two database reads rather than a model call. Only when there is no
stored gist yet does it fall back to a sample search. A search with three or more
results streams a fresh topic gist into the same box, under the search bar;
clearing the search puts the day's gist back.

Design follows `premiumtimes-content-api.vercel.app` — same newsprint palette,
Archivo/Newsreader/IBM Plex Mono, ruled-paper background — so the two read as one
family. Machine-facing is still the product; this page exists so the work can be
shown to a newsroom without a terminal.

OpenAPI docs remain at `/docs`.

## Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /` | Front page — live search demo and developer view |
| `GET /v1` | Endpoint index, with the caller's plan and remaining rate limit |
| `GET /v1/health` | Liveness, corpus size, failing sources, the gist writer's last outcome |
| `GET /v1/sources` | Newsrooms indexed, how each is read and when it was last read |
| `GET /v1/sources/{id}` | One newsroom, with its article count and date range |
| `GET /v1/articles` | Unified feed; filter by source, section, language, wire, date |
| `GET /v1/articles/{id}` | One article |
| `GET /v1/articles/{id}/revisions` | Every observed edit, including corrections |
| `GET /v1/search` | Full-text over headline, dek, snippet and entities |
| `GET /v1/search/gist` | Streamed gist of recent coverage on a topic (NDJSON) |
| `GET /v1/clusters` | Same story across outlets, each with its gist; filter by size and recency (`hours`), `sort=recent` or `size` |
| `GET /v1/clusters/{id}` | One story: its gist and every outlet's version |
| `GET /v1/admin/ingest` | Scheduled job only (needs `CRON_SECRET`): fetch every enabled newsroom |
| `GET /v1/admin/digest` | Scheduled job only (needs `CRON_SECRET`): group stories, write up to 25 gists |

Auth is `X-API-Key` or `Authorization: Bearer`. Issue keys with
`python -m app.keys issue "Name" --plan pro --rate 600`; a key without `--rate`
gets 120 requests a minute (`ROUTER_DEFAULT_RATE`). Anonymous reads are allowed
by default at 30 a minute (`ROUTER_ANON_RATE`); set `ROUTER_ALLOW_ANON=0` in
production. `/v1/health` needs no key.

## Where the articles come from

Everything is read from what the newsrooms publish openly for anyone to read.
Every record carries the outlet's name and links back to its own page.
`app/sources.yaml` is the roster; `enabled: false` means the router knows an
outlet but cannot currently read it.

| Mechanism | Adapter | Outlets |
|---|---|---|
| Open WordPress REST (`/wp-json/wp/v2`) | `wordpress` | Premium Times, The ICIR, Ripples Nigeria, Punch, Leadership, Peoples Gazette, Daily Trust, Nairametrics, Nigerian Tribune, ThisDay, The Sun, BusinessDay, New Telegraph, Blueprint, Daily Post, The Whistler, News Agency of Nigeria, Arise News, TVC News |
| RSS feed | `rss` | Vanguard, Channels Television, Legit.ng, Sahara Reporters |
| — | — | TheCable, off: both blocked |

WordPress REST is preferred wherever it answers, because it can be paged back
through a whole day. A feed only holds the latest 10–30 items — Punch's covers
about two hours — so once-a-day reads of feeds miss most of the news. Vanguard
and Channels are on their feeds only because their wp-json returns 403
(Cloudflare); Legit.ng and Sahara Reporters are not WordPress sites. TheCable
blocks both (probed 2026-08-21 and 2026-08-27).

Two outlets need special handling. The Whistler's server stalls for 90s+ when
asked to bundle author, image and categories (`_embed`), so it is marked
`embed: false` and its author and image come from Yoast's metadata. ThisDay's
server refuses page 2 with a stale page count while later pages exist, so a
refused page is retried by offset before it is taken as the end.

The scheduled read takes up to 200 reports per outlet, resuming two hours before
that outlet's last clean read. Outlets are fetched side by side (8 at a time)
and saved one at a time, so a normal day across all 23 takes about 40 seconds
locally, and even a read of every outlet from empty takes under a minute.
Busy outlets publish a lot: on 2026-10-04 Leadership posted 184 reports, Punch
167 and Tribune 113.

`python -m app.remap_punch` is a one-off, to be run once after the first
WordPress read of Punch. Punch's RSS rows were keyed on a hash of the URL rather
than the post id, so it folds each old row into its new twin by URL.

## Stories and gists

**Grouping the same story.** `app/cluster.py` looks at articles from the last
72 hours and puts two of them in the same story when any one of these holds:

- their headlines share at least 3 words, and those shared words make up at
  least half of the shorter headline;
- the publishers tagged both with at least 2 of the same people, places or
  organisations;
- both are wire copy from the same agency, and at least half of all the words
  across the two headlines are shared.

Common filler words ("says", "Nigeria", "breaking") don't count, and neither do
tags that are only filler. Words in a publisher's tags count as headline words.
Only articles from different outlets are matched, and sponsored or retracted
items are never grouped. Each run only places articles that don't have a story
yet, so earlier groupings never get reshuffled.

**Writing the gist.** Once a story has at least two articles, the gist writer
produces a short neutral summary plus one note per outlet, returned in a fixed
shape and checked before anything is stored. The model sees only what the API
itself serves: headline and outlet name, plus a dek or snippet where the outlet
provides one. Each gist records a fingerprint of its
inputs, so a story whose coverage hasn't changed costs nothing on the next run.
Switching to a different model rewrites it. A run writes at most 25 gists,
newest stories first, and leaves the rest for the next run. How the last run
went (worked, not configured, offline, errors) is saved and shown in
`/v1/clusters` and `/v1/health`, so a missing gist comes with a reason.

**Topic gists.** `GET /v1/search/gist?q=…` summarises recent coverage of any
search, over the last 7 days by default (`days`, 1–30). It reads the
best-matching 2 to 12 articles, leaving out sponsored and retracted items, and
streams the text as it is written: NDJSON, one `meta` line, then `delta` lines,
then `done`. If there is too little coverage or no writer available, it sends a
single `status` line instead. A summary is remembered for an hour, keyed on
the articles it read rather than the words typed, so "nysc" and "nysc camp"
share one when they match the same reports. That memory is per running
instance; the serving path cannot write, so it has nowhere durable to cache.

Because visitors trigger these, fresh ones are rationed: 5 per visitor in any
10 minutes, 20 a day, and 300 a day across everyone (per instance). Cached
answers don't count. Past a limit the stream sends a `busy` status line; the
search results themselves are unaffected. Set `GROQ_SEARCH_API_KEY` to give
these their own Groq key, so spam that uses up its free allowance cannot break
the morning digest. Unset, they share `GROQ_API_KEY`.

## Data notes that cost real debugging time

- **Timestamps are not trustworthy.** WordPress `date` is site-local (WAT, UTC+1)
  and `date_gmt` is UTC without an offset suffix. Both are stored: `published_at`
  is normalised UTC, `published_at_reported` keeps the publisher's claim, and
  `first_seen_at` records when the router saw it. A publisher edit can never move
  `first_seen_at`.
- **Excerpts arrive doubled.** Share-button plugins that filter `get_the_excerpt`
  emit the text twice. Left alone this doubles every dek and poisons text
  similarity for clustering. `collapse_duplicate()` handles it.
- **`modified` is not a reliable change signal.** Changes are detected by hashing
  the body, so a silent edit is caught even when the publisher leaves `modified`
  untouched. Each change writes an `article_revisions` row naming the fields that
  moved.
- **8% of the Premium Times feed is advertorial.** Sponsored items are flagged and
  excluded from `/v1/articles` by default (`include_sponsored=true` to see them).
  They must never be clustered with reporting.
- **Publisher tags are useful but not sufficient for clustering.** Outlets do tag
  with entity names, and those are indexed and searchable. But on the first real
  cross-outlet pair the router caught, tag overlap was almost nil: Premium Times
  tagged `President Bola Tinubu` and `Independent Corrupt Practices and other
  Related Offences Commission (ICPC)` where Ripples tagged `Tinubu` and
  `fake agency`. Same story, 21 minutes apart, no usable tag intersection.
  Headline token overlap plus publication time was the far stronger signal.
- **Bodies are not stable between fetches.** Ripples' theme injects ad
  containers whose element ids are regenerated on every request. Those ids sat
  inside `<script>` blocks that plain tag-stripping left behind as text, so the
  content hash moved on every ingest and all 80 articles logged a correction
  that never happened. `strip_html()` now drops `<script>`, `<style>` and
  `<noscript>` bodies outright. Two consecutive ingests should report every
  article unchanged — that is the check worth running after adding an outlet.
- **Tag vocabularies are per-outlet and full of page furniture.** Premium Times
  emits `Headline1`, Ripples `#featured`, ICIR `Billboard Article`. Filtered in
  `_is_layout_tag()`; expect to extend it with every outlet added.

## Storage

Postgres, hosted on Neon. Two decisions in `app/schema.sql` are worth knowing
before changing anything:

**Timestamps are TEXT, not `timestamptz`.** The router receives times it does
not trust — publishers backdate, mix WAT with UTC, and revise timestamps after
the fact. Storing the exact string received keeps that visible rather than
laundering it through a type conversion. ISO-8601 UTC sorts correctly as text,
so ordering, range filters and keyset pagination all behave normally.

**Flags are INTEGER 0/1, not `boolean`,** so `enabled = 1` reads the same here
as it did before the move from SQLite.

Search is a generated `tsvector` column with a GIN index, weighted so a term in
the headline outranks the same term in a snippet. Queries go through
`websearch_to_tsquery`, which parses what people actually type — bare words,
"quoted phrases", `OR`, a leading minus — and cannot be made to throw by stray
punctuation, so there is no sanitising pass in front of it.

Coming from the old SQLite file:

```bash
./.venv/bin/python -m app.migrate_from_sqlite --sqlite router.db
```

That preserves ids, `first_seen_at` and the full revision history, none of which
can be recovered by re-ingesting — publishers only ever serve what is current.

## Deployment

```bash
vercel deploy --prod
```

Only code ships. The database is a separate service, so a deploy no longer
carries the index with it and the site is never frozen between deploys.

The API reads as `router_reader`, a role holding `SELECT` and nothing else, so
a write from the serving path fails on permissions rather than on the honour
system. Do not be tempted to enforce that with
`SET default_transaction_read_only = on` instead: the setting survives on the
server connection after a pooler takes it back, and the next caller inherits
it — which is how the nightly ingest first broke. Neon's pooler refuses the
equivalent startup option outright. Credentials are the only mechanism that
travels correctly through a pooler.

**The serving path never writes.** `app.state.conn` is opened with
`default_transaction_read_only = on`. Creating the schema and syncing the source
registry belong to `python -m app.setup`; ingestion opens its own connection.

**Ingestion runs on a schedule, not on your laptop.** `vercel.json` has two
Vercel Cron jobs: `GET /v1/admin/ingest` at 05:00 UTC — 6am in Lagos — fetches
every enabled newsroom, then `GET /v1/admin/digest` at 05:30 UTC groups the
morning's articles into stories and writes up to 25 gists. Those two endpoints
are the only write paths in the deployed application. Both refuse anyone who
does not present `CRON_SECRET`, which Vercel sends as a bearer token, and with
the secret unset they refuse everyone, rather than defaulting open.

Gists on the deployed site come from Groq: `GROQ_API_KEY` must be set on
Vercel. `ROUTER_GIST_BACKEND` defaults to `groq`, so it only needs setting to
switch to Claude.

Use the **pooled** connection string, the one whose host contains `-pooler`.
Serverless spawns many short-lived instances, and each opening its own direct
connection will exhaust Postgres long before traffic does.

## Tests

```bash
./.venv/bin/python -m pytest tests/ -q                    # logic tests only
TEST_DATABASE_URL='postgresql://…' ./.venv/bin/python -m pytest tests/ -q
```

Covers timezone normalisation, excerpt de-duplication, wire detection, volatile
ad markup, RSS parsing, revision tracking (including that `first_seen_at`
survives a publisher edit) and that every outlet is served the same fields. On the story side, it covers
the matching rule (filler words and filler tags carry no weight), that a re-run
reshuffles nothing, that sponsored copy and a single outlet never form a
story, and the cluster listing's `sort` and `hours`. For gists, it covers that
the prompt carries every outlet's description, that the input fingerprint moves with
membership and edits, and each writer (Claude, Groq, Ollama) against a fake
model with no network. It also checks how an offline or unconfigured writer is
reported, and the topic gist's article selection and stream lines.

Storage tests need a real Postgres and skip without `TEST_DATABASE_URL` — the
schema uses a generated `tsvector` and Postgres text search, so a stand-in would
be testing something the application does not run. A Neon branch makes a good
scratch database: it is a copy-on-write clone, so it costs nothing to throw away.

## Not built yet

- Clustering recall. Matching runs on headline words, publisher entity tags and
  wire markers, because bodies are never stored. That favours precision: two
  outlets writing the same event under very different headlines will sometimes
  stay apart, which is the honest failure mode for something the site presents
  as "the same story".
- Retraction detection (the columns and endpoint exist; nothing sets them).
- Distributed rate limiting — the limiter is in-process, so it is per-instance.
  Now that Postgres is there, it is the obvious place to put a shared counter.
  The topic-gist cache is per-instance for the same reason.
