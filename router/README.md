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

SQLite with FTS5. SQL is written to stay portable to Postgres — the only
SQLite-specific block is the FTS virtual table, which becomes a `tsvector` column
plus a GIN index.

## Deployment

Vercel, from the CLI rather than from git:

```bash
./.venv/bin/python -m app.ingest --limit 80    # refresh the index
./.venv/bin/python -c "import sqlite3; c=sqlite3.connect('router.db'); \
  c.execute('PRAGMA journal_mode=DELETE'); c.execute('VACUUM')"
rm -f router.db-wal router.db-shm
vercel deploy --prod
```

The database ships inside the deployment. That is why the deploy is a CLI upload
and not a git push: `router.db` is git-ignored, and `.vercelignore` deliberately
does not exclude it. Vercel falls back to `.gitignore` when `.vercelignore` is
absent, which would silently ship an empty index.

A serverless filesystem is read-only, and SQLite writes even to serve reads — the
WAL journal and the startup source sync both touch disk. So `config._db_path()`
copies the bundled database to `/tmp` once per cold start and serves from there.

**The index is frozen at deploy time.** New articles need a re-ingest and a
redeploy. Moving to Postgres would let ingestion run against the live site; that
is the next step if the site needs to stay current on its own.

## Tests

```bash
./.venv/bin/python -m pytest tests/ -q
```

Covers timezone normalisation, excerpt de-duplication, wire detection, revision
tracking (including that `first_seen_at` survives a publisher edit) and rights
enforcement.

## Not built yet

- **Clustering.** Schema and endpoints exist; nothing populates `cluster_id`.
- **Tier 1/3/4 adapters.**
- Retraction detection (the columns and endpoint exist; nothing sets them).
- Distributed rate limiting — the limiter is in-process, so it is per-instance.
