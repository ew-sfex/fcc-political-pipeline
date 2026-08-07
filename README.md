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

1. `src/fcc_client.py` fetches each configured station's political-file RSS
   (Atom) feed via a headless-browser client (see "Data source" below for
   why) and parses out filing metadata.
2. `src/ingest.py` orchestrates: for each new filing (not already in the
   DB), write a row to the `filings` table with purchaser, category, filing
   date, and the file's `download_url` for later retrieval.
3. `.github/workflows/ingest.yml` runs this on a schedule via GitHub Actions.

## Data source: per-station RSS feeds

`src/fcc_client.py` pulls each station's political-file upload history from
its FCC OPIF RSS (Atom) feed:

```
https://publicfiles.fcc.gov/[tv|fm|am]-profile/[callsign]/rss/
```

This was confirmed working by direct browser inspection on 2026-08-05. An
earlier version of this project queried `https://www.fcc.gov/search/api`
instead — that turned out to be the wrong endpoint entirely (the general
fcc.gov website content search, not the political file index), and is why
you may see references to a JSON search API filter scheme in old notes for
this project. The RSS feed is the correct, working source.

Useful side effect: the feed's category path (e.g. `Political
Files/2026/Non-Candidate Issue Ads/BOLD America`) ends in the purchaser /
committee name, so `purchaser` is populated directly at ingest time from the
FCC's own taxonomy — no LLM extraction needed for that field.

Each station's feed is its **entire** public file upload history, not just
political ads — EEO reports, ownership filings, issues/programs lists, etc.
all show up too (confirmed 2026-08-07: only ~57% of a typical feed is
political). `ingest.py` filters to entries whose `category_path` starts with
`Political Files/` via `FccFiling.is_political` before writing to the DB.

FCC also publishes an official, documented, no-auth-required JSON API
(publicfiles.fcc.gov/developer, OpenAPI spec at
`/api/manager//json/apis.json`) covering the same folder/file metadata
(`/api/manager/folder/id/{folderId}.json`, `/api/manager/file/id/{fileId}.json`).
It's not currently used here since the RSS feed already gives a flat,
per-station "everything uploaded" view in one request, whereas the JSON API
is hierarchical (year → category → purchaser folders) and would need
recursive traversal to enumerate. Worth revisiting if the RSS feed's shape
ever changes.

### Akamai and headless browsing

Both `www.fcc.gov` and `publicfiles.fcc.gov` sit behind an Akamai
bot-detection layer. Plain `requests` calls get 403'd even with a
browser-like `User-Agent` header — Akamai is fingerprinting the TLS/JS
layer, not just headers. `fcc_client.py` works around this for the **RSS
feed** by fetching via a real headless Chromium (Playwright) instead of
`requests`. Confirmed working live as of 2026-08-07.

**Before relying on this at scale, run `scripts/probe_api.py`** to confirm
the feed's current shape:
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

The workflow in `.github/workflows/ingest.yml` runs daily. It only needs the
`DATABASE_URL` secret set in the repo's Settings → Secrets and variables →
Actions (Drive-related secrets aren't used yet — see "PDF download status").

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
src/fcc_client.py                FCC OPIF RSS feed client (Playwright-based)
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
