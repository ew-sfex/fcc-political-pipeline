# FCC Political Filings Pipeline

Pulls political-file filing **metadata** from the FCC's Online Public
Inspection File (OPIF) system for a configured list of stations (default:
Bay Area) and indexes it in a database for filtering/dashboarding. PDF
storage in Drive is designed for but currently deferred — see "PDF download
status" below.

## Status

This is the **Phase 1 MVP**, currently running in **metadata-only** mode:
ingest filing metadata (purchaser, category, filing date, size, FCC IDs) for
every station, without fetching the PDF itself. Phase 2 (LLM extraction of
structured ad-buy data from the PDFs) and Phase 3 (dashboard) build on top of
the database this phase populates — see `src/db.py` for the schema, it
already has a `filings` table ready to receive extraction output later, plus
a `download_url` on every row so PDFs can be backfilled once download access
is sorted out.

## How it works

1. `src/fcc_client.py` resolves each configured station's numeric FCC entity
   ID (from its RSS feed — see below), then recursively walks that station's
   **complete** `Political Files` folder tree via FCC's JSON folder API,
   returning every filing found.
2. `src/ingest.py` orchestrates: for each filing not already in the DB,
   write a row to the `filings` table with purchaser, category, filing
   date, and the file's `download_url` for later retrieval. Re-running is
   safe and idempotent — already-ingested filings are skipped.
3. `.github/workflows/ingest.yml` runs this 3×/day via GitHub Actions.

## Data sources

The client uses two FCC endpoints, for two purposes:

**1. Folder-walk JSON API (primary — file discovery).** FCC publishes an
official, documented, no-auth-required JSON API
(publicfiles.fcc.gov/developer, OpenAPI spec at `/api/manager//json/apis.json`).
`walk_political_files()` uses it to recursively enumerate a station's
`Political Files` tree (year → category → purchaser → files). This returns
**complete** folder contents — nothing can silently fall off between runs
regardless of upload volume. Each folder carries a recursive `file_count`,
used to prune empty branches. Plain `requests` works here; no browser needed.

Useful side effect: the folder path (e.g. `Political Files/2026/Non-Candidate
Issue Ads/BOLD America`) ends in the purchaser / committee name, so
`purchaser` is populated directly at ingest time from FCC's own taxonomy — no
LLM extraction needed for that field.

**2. Per-station RSS (Atom) feed (secondary — entity-ID bootstrap only).**

```
https://publicfiles.fcc.gov/[tv|fm|am]-profile/[callsign]/rss/
```

Used only to look up a station's numeric entity ID (embedded in each feed
entry's title, e.g. "tv Entity 34470 uploaded…"), which the folder API
needs — there's no working public callsign→entity-ID search endpoint
otherwise. **The feed itself is capped at the 10 most recent uploads of any
category**, no matter how much history exists (KGO-TV has 900+ political
files back to 2017; its feed shows 10), which is exactly why it can't be the
primary source — hence the folder walk above. An earlier version of this
project instead queried `https://www.fcc.gov/search/api`, which turned out to
be the wrong endpoint entirely (general fcc.gov site search, not the file
index) — ignore any old notes referencing it.

Note: the walk is rooted at the Political Files tree, so everything it
returns is political by folder location. FCC's per-folder `folder_path`
label is occasionally inconsistent with a folder's actual tree position
(e.g. a genuine political-ad NAB form filed under a folder labeled "Issues
and Programs Lists/…"), so a small number of rows may carry a non-`Political
Files/` `category_path` even though they're real political filings reached
via the political tree. `FccFiling.is_political` checks the label string, so
it will read False for those edge cases.

### Akamai and headless browsing

`publicfiles.fcc.gov` sits behind an Akamai bot-detection layer. The **JSON
folder API** is unaffected — plain `requests` works. Only the **RSS feed
origin** 403s plain `requests` even with a browser-like `User-Agent` (Akamai
fingerprints the TLS/JS layer, not just headers), so `fcc_client.py` fetches
the feed via a real headless Chromium (Playwright). Confirmed live 2026-08-07.

**To sanity-check the RSS feed's shape** (used for probing, not ingest):
```bash
python3 scripts/probe_api.py KGO-TV TV
```

## PDF download status: blocked, not yet solved

Unlike the RSS feed, the actual file-download endpoint
(`/api/manager/download/{folderId}/{fileId}.pdf` — both the documented API
route and the equivalent link inside the RSS feed) returns 403 for **any
automated client**, including a full non-headless Chromium session driven by
Playwright. Confirmed by testing the exact same URL by hand in an ordinary,
non-automated Chrome tab, where it downloads fine — so this isn't a
network/IP block, it's Akamai specifically distinguishing an
automation-controlled browser session from a human-driven one, on the
download path only (the JSON metadata API has no such block).

Getting past that distinction would require hiding automation fingerprints
(`navigator.webdriver`, CDP artifacts, etc.) from a real browser session —
deliberately defeating anti-bot protection, which this project won't do.

Current plan: emailed developer@fcc.gov (2026-08-07) asking whether there's
a sanctioned way to fetch file content programmatically. Until/unless that
turns up something, `ingest.py` only records metadata; PDFs need to be
fetched by hand via `download_url` on each row. Phase 2 (LLM extraction from
PDF bodies) is blocked on this too.

## Setup

### 1. Database (Supabase Postgres recommended)

1. Create a free project at supabase.com.
2. Grab the connection string (Project Settings → Database → Connection string,
   "URI" format, use the pooled connection for serverless/Actions use).
3. Put it in the GitHub secret `DATABASE_URL`.

For local testing without Supabase, just leave `DATABASE_URL` unset — it
defaults to a local `pipeline.db` SQLite file.

### 2. Bay Area station list

Edit `config/bay_area_stations.yaml`. This is a starter list of Bay Area
broadcast callsigns — **verify/expand it**; the FCC's own entity search
(https://publicfiles.fcc.gov/find) is the source of truth for which stations
have online political files in your target market.

### 3. Environment variables (for local runs)

Copy `.env.example` to `.env` and fill in values (optional in metadata-only
mode — everything defaults to local SQLite with no external creds needed).
Locally:

```bash
pip install -r requirements.txt
playwright install chromium
python -m src.ingest
```

### 4. GitHub Actions

The workflow in `.github/workflows/ingest.yml` runs 3×/day (~7am/noon/4pm
Pacific). It only needs the `DATABASE_URL` secret set in the repo's Settings
→ Secrets and variables → Actions (Drive-related secrets aren't used yet —
see "PDF download status"). Each run re-walks each station's full Political
Files tree (~3–4 min total for the default 14-station list) and writes only
filings not already in the DB — safe to run as often as you like.

### 5. Google Drive access (not yet needed)

Deferred until PDF downloads are unblocked (see above). When that's solved,
`src/drive_client.py` is already written for it — use a service account
(not personal OAuth, since this runs unattended):

1. In Google Cloud Console, create a project, enable the Drive API.
2. Create a service account, download its JSON key.
3. Share the destination Drive folder with the service account's email
   (found in the JSON key as `client_email`) as an Editor.
4. Put the JSON key contents in the GitHub repo secret `GOOGLE_SERVICE_ACCOUNT_JSON`.
5. Put the destination folder's ID (from its URL) in the secret `DRIVE_ROOT_FOLDER_ID`.

Note: Drive file `properties` (what this uses) work on any Drive, including
non-Workspace ones, as long as the service account has edit access to the
folder. This is different from Drive **Labels**, which require Workspace admin
setup — we're deliberately not using Labels here for portability, but you can
layer them on later if useful.

## Repo layout

```
config/bay_area_stations.yaml   station list + market metadata
src/config.py                   env/config loading
src/fcc_client.py                FCC OPIF client: JSON folder-walk + RSS bootstrap
src/drive_client.py              Drive upload + properties tagging (not yet wired in)
src/db.py                        SQLAlchemy models + engine (SQLite or Postgres)
src/ingest.py                    orchestration entrypoint (metadata-only)
scripts/probe_api.py             manual script to sanity-check the FCC feed shape
.github/workflows/ingest.yml     scheduled run
```

## Next phases (not built yet)

- **PDF storage**: wire `drive_client.py` back into `ingest.py` once file
  downloads are unblocked (see "PDF download status").
- **Phase 2 — LLM extraction**: for each filing PDF, call an LLM with a fixed
  JSON schema (advertiser, spend, flight dates, document type, etc.) and
  write results into the `filings` table's extraction columns. Given the
  variance in filing formats, plan for a confidence/review flag rather than
  trusting every extraction blindly. Blocked on PDF storage.
- **Phase 3 — Dashboard**: a small app (Streamlit is the fastest path) reading
  directly from the same Postgres database to let users filter/search.
