# FCC Political Filings Pipeline

Pulls political-file filings from the FCC's Online Public Inspection File (OPIF)
system for a configured list of stations (default: Bay Area), stores the PDFs in
Google Drive tagged with structured metadata (Drive file `properties`), and
indexes everything in a database for filtering/dashboarding.

## Status

This is the **Phase 1 MVP**: ingest + organize. Phase 2 (LLM extraction of
structured ad-buy data from the PDFs) and Phase 3 (dashboard) build on top of
the database this phase populates — see `src/db.py` for the schema, it already
has a `filings` table ready to receive extraction output later.

## How it works

1. `src/fcc_client.py` queries the FCC OPIF search API for each configured
   station callsign, filtered to the political file folder.
2. `src/ingest.py` orchestrates: for each new filing (not already in the DB),
   download the PDF, upload it to a Drive folder, tag it with `properties`
   (purchaser placeholder, station, market, filing date, FCC IDs), and write a
   row to the `filings` table.
3. `.github/workflows/ingest.yml` runs this on a schedule via GitHub Actions.

## ⚠️ Important caveat on the FCC API

The FCC does not publish a full parameter reference for the OPIF file search
API — `src/fcc_client.py` is built against the request/response shape that's
been observed in the wild (see comments in that file), not an official spec.
**Before relying on this for real ingestion, run `scripts/probe_api.py`
against the live API and confirm the field names and filter behavior still
match** — FCC has changed this system's backend before (it was taken offline
and relaunched as recently as late 2025) and undocumented APIs can shift
without notice.

## Setup

### 1. Google Drive access (service account)

Because this runs unattended in GitHub Actions, use a **service account**,
not your personal OAuth login:

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

### 2. Database (Supabase Postgres recommended)

1. Create a free project at supabase.com.
2. Grab the connection string (Project Settings → Database → Connection string,
   "URI" format, use the pooled connection for serverless/Actions use).
3. Put it in the GitHub secret `DATABASE_URL`.

For local testing without Supabase, just leave `DATABASE_URL` unset — it
defaults to a local `pipeline.db` SQLite file.

### 3. Bay Area station list

Edit `config/bay_area_stations.yaml`. This is a starter list of Bay Area
broadcast callsigns — **verify/expand it**; the FCC's own entity search
(https://publicfiles.fcc.gov/find) is the source of truth for which stations
have online political files in your target market.

### 4. Environment variables (for local runs)

Copy `.env.example` to `.env` and fill in values. Locally:

```bash
pip install -r requirements.txt
python -m src.ingest
```

### 5. GitHub Actions

The workflow in `.github/workflows/ingest.yml` runs daily. It expects the
three secrets above (`GOOGLE_SERVICE_ACCOUNT_JSON`, `DRIVE_ROOT_FOLDER_ID`,
`DATABASE_URL`) to be set in the repo's Settings → Secrets and variables →
Actions.

## Repo layout

```
config/bay_area_stations.yaml   station list + market metadata
src/config.py                   env/config loading
src/fcc_client.py                FCC OPIF API client
src/drive_client.py              Drive upload + properties tagging
src/db.py                        SQLAlchemy models + engine (SQLite or Postgres)
src/ingest.py                    orchestration entrypoint
scripts/probe_api.py             manual script to sanity-check the FCC API shape
.github/workflows/ingest.yml     scheduled run
```

## Next phases (not built yet)

- **Phase 2 — LLM extraction**: for each filing PDF, call an LLM with a fixed
  JSON schema (advertiser, spend, flight dates, document type, etc.) and
  write results into the `filings` table's extraction columns. Given the
  variance in filing formats, plan for a confidence/review flag rather than
  trusting every extraction blindly.
- **Phase 3 — Dashboard**: a small app (Streamlit is the fastest path) reading
  directly from the same Postgres database to let users filter/search.
