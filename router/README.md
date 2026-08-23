# Nigerian News Router

**Live:** https://news-router.vercel.app · [API docs](https://news-router.vercel.app/docs)

One read API across Nigerian newsrooms. Every ingestion tier normalises into a
single schema, so a consumer never learns whether a story arrived from an
installed content API, an open WordPress endpoint, or RSS.

**Metadata only.** Headline, dek, byline, timestamps, canonical URL, section,
snippet and thumbnail. Article bodies are read transiently during ingestion — to
hash for change detection and to spot wire copy — and are never stored or served,
at any tier.

## Run it

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements-dev.txt

export DATABASE_URL='postgresql://…'          # see Storage below
./.venv/bin/python -m app.setup               # create tables, load sources.yaml
./.venv/bin/python -m app.ingest --source premium-times --limit 60
./.venv/bin/python -m uvicorn app.main:app --port 8099
```

Then open `http://localhost:8099/`.

## The front page

`/` serves a single static page from `app/static/index.html` — no build step, no
second deployment. It is a working demonstration rather than a product surface:
live search across every indexed newsroom, the roster with each newsroom's
agreement shown, and a developer view whose endpoints run against the live index
and print the real response with timing and payload size.

Design follows `premiumtimes-content-api.vercel.app` — same newsprint palette,
Archivo/Newsreader/IBM Plex Mono, ruled-paper background — so the two read as one
family. Machine-facing is still the product; this page exists so the work can be
shown to a newsroom without a terminal.

OpenAPI docs remain at `/docs`.

## Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /` | Front page — live search demo and developer view |
| `GET /v1/health` | Liveness, corpus size, failing sources |
| `GET /v1/sources` | Newsrooms indexed, with tier and licensing state |
| `GET /v1/articles` | Unified feed; filter by source, section, language, wire, date |
| `GET /v1/articles/{id}` | One article |
| `GET /v1/articles/{id}/revisions` | Every observed edit, including corrections |
| `GET /v1/search` | Full-text over headline, dek, snippet and entities |
| `GET /v1/clusters` | Same story across outlets *(schema ready, clustering not yet built)* |

Auth is `X-API-Key` or `Authorization: Bearer`. Issue keys with
`python -m app.keys issue "Name" --plan pro --rate 600`. Anonymous reads are
allowed by default at a lower limit; set `ROUTER_ALLOW_ANON=0` in production.

## Licensing is enforced per record, not per request

A single response mixes sources on different agreements, so rights are applied to
each record as it is serialised. Fields outside a source's grant come back as
`null` with an explicit `rights` block, so a consumer can tell *"withheld"* from
*"the publisher never supplied it"*:

```json
"rights": { "dek": true, "snippet": true, "image": true, "body": false }
```

`app/sources.yaml` is the source of truth for endpoints and rights. `enabled:
false` means the router knows about an outlet but will not ingest it — **an open
`/wp-json` endpoint is not permission.** Three outlets are enabled today:
Premium Times, The ICIR and Ripples Nigeria.

Where each agreement actually stands is deliberately kept out of this repo and
out of the API. It lives in `app/sources.local.yaml`, which is git-ignored;
copy `app/sources.local.example.yaml` to start one. Absent that file every
source reads as `none`, which is the safe default — it grants nothing. The
`rights_*` flags in `sources.yaml` are what decide what the API will serve.

> Ripples Nigeria was switched on by request rather than from a signed
> agreement. Confirm it before relying on the index.

## Ingestion tiers

| Tier | Mechanism | Adapter | Status |
|---|---|---|---|
| 1 | Installed content API — canonical IDs, webhooks | `content_api` | not built |
| 2 | Open WordPress REST | `wordpress` | **working** — 3 outlets live |
| 3 | Gated outlets, metadata only | — | not built |
| 4 | RSS fallback | `rss` | not built |

Probe of 11 Nigerian outlets (2026-08-21): 9 serve `/wp-json/wp/v2/posts`.
Vanguard and TheCable return 403 — Cloudflare or REST disabled — and will need a
tier-1 install or an agreed allowlist rather than open polling.

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

**The serving path never writes.** `app.state.conn` is opened with
`default_transaction_read_only = on`. Creating the schema and syncing the source
registry belong to `python -m app.setup`; ingestion opens its own connection.

**Ingestion runs on a schedule, not on your laptop.** `vercel.json` has Vercel
Cron calling `GET /v1/admin/ingest` at 05:00 UTC — 6am in Lagos. That endpoint is
the only write path in the deployed application and refuses anyone who does not
present `CRON_SECRET`, which Vercel sends as a bearer token. With the secret
unset it refuses everyone, rather than defaulting open.

Use the **pooled** connection string, the one whose host contains `-pooler`.
Serverless spawns many short-lived instances, and each opening its own direct
connection will exhaust Postgres long before traffic does.

## Tests

```bash
./.venv/bin/python -m pytest tests/ -q                    # logic tests only
TEST_DATABASE_URL='postgresql://…' ./.venv/bin/python -m pytest tests/ -q
```

Covers timezone normalisation, excerpt de-duplication, wire detection, volatile
ad markup, revision tracking (including that `first_seen_at` survives a
publisher edit) and rights enforcement.

Storage tests need a real Postgres and skip without `TEST_DATABASE_URL` — the
schema uses a generated `tsvector` and Postgres text search, so a stand-in would
be testing something the application does not run. A Neon branch makes a good
scratch database: it is a copy-on-write clone, so it costs nothing to throw away.

## Not built yet

- **Clustering.** Schema and endpoints exist; nothing populates `cluster_id`.
- **Tier 1/3/4 adapters.**
- Retraction detection (the columns and endpoint exist; nothing sets them).
- Distributed rate limiting — the limiter is in-process, so it is per-instance.
  Now that Postgres is there, it is the obvious place to put a shared counter.
