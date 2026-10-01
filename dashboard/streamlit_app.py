"""Read-only newsroom dashboard for the FCC political filings database.

A shareable web page: anyone with the link can browse, sort, filter, and
search every ingested filing - no login, no database access. It only ever
runs SELECTs, so viewers can't change or delete anything.

Deploy on Streamlit Community Cloud (free):
  - Main file path:  dashboard/streamlit_app.py
  - Secret:          DATABASE_URL = "<Supabase pooled connection string>"
See README.md "Newsroom dashboard" for the full click-through.

Run locally against the SQLite test DB:
  streamlit run dashboard/streamlit_app.py
(falls back to sqlite:///pipeline.db when DATABASE_URL is unset)
"""
from __future__ import annotations

import os
import re
from datetime import date

import pandas as pd
import streamlit as st
from sqlalchemy import create_engine


def _database_url() -> str:
    # st.secrets on Streamlit Cloud; env var locally. Fall back to the local
    # SQLite file so the app runs with no config during development.
    url = ""
    try:
        url = st.secrets.get("DATABASE_URL", "")
    except Exception:
        url = ""
    url = url or os.environ.get("DATABASE_URL", "") or "sqlite:///pipeline.db"
    # Supabase/Heroku hand out "postgres://"; SQLAlchemy needs "postgresql://".
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    # Pin the psycopg (v3) driver explicitly so the dialect always resolves to
    # the one we install (psycopg[binary]), regardless of what SQLAlchemy would
    # pick by default on a given rebuild. Normalize any ...+psycopg2 too.
    if url.startswith("postgresql+psycopg2://"):
        url = "postgresql+psycopg://" + url[len("postgresql+psycopg2://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


@st.cache_resource
def _engine():
    return create_engine(_database_url(), future=True)


@st.cache_data(ttl=600)
def load_filings() -> pd.DataFrame:
    """Load all filings, newest first. Cached for 10 min so the page is snappy
    and we're not re-querying on every widget interaction.

    `entity_id` was added after the table first shipped; the ingest migrates
    the DB, but the dashboard reads directly (no migration), so tolerate the
    column not existing yet during a deploy window and synthesize it as null."""
    cols = "callsign, purchaser, category_path, file_name, filed_date, download_url, service, market"
    try:
        df = pd.read_sql(f"SELECT {cols}, entity_id FROM filings ORDER BY filed_date DESC", _engine())
    except Exception:
        df = pd.read_sql(f"SELECT {cols} FROM filings ORDER BY filed_date DESC", _engine())
        df["entity_id"] = None
    df["filed_date"] = pd.to_datetime(df["filed_date"], errors="coerce")
    return df


def _race_type(category_path: str) -> str:
    """Second segment of the category path is the broad bucket, e.g.
    'Political Files/2026/Federal/...' -> 'Federal'."""
    parts = [p for p in (category_path or "").split("/") if p]
    return parts[2] if len(parts) > 2 else "(uncategorized)"


_SERVICE_SLUG = {"TV": "tv-profile", "AM": "am-profile", "FM": "fm-profile", "CABLE": "cable-profile", "DBS": "dbs-profile"}
_DOWNLOAD_FOLDER_RE = re.compile(r"/manager/download/([^/]+)/")

# Advertiser/committee derivation - kept in sync with src/fcc_client.py so rows
# stored before the ingest fix get the same treatment at display time.
_PLATFORM_PREFIX_RE = re.compile(r"^[A-Z]{2,5} - ")
_DOC_TYPE_LEAVES = {
    "contracts", "contract", "invoices", "invoice", "orders", "order",
    "traffic", "traffic instructions - creative piq",
    "nab and disclosures", "nab", "nab form", "nab forms",
    "election forms nab loa fec etc", "terms and disclosures",
    "agreements", "agreement", "disclosures",
}


def _purchaser(category_path: str) -> str:
    """Advertiser/committee = first non-document-type segment from the leaf up,
    with the cable ad-platform prefix stripped. Some stations nest Contracts/
    Invoices/Traffic/NAB folders under the committee, so the leaf is often a
    document type rather than the advertiser."""
    parts = [p.strip() for p in (category_path or "").split("/") if p.strip()]
    for seg in reversed(parts):
        name = _PLATFORM_PREFIX_RE.sub("", seg).strip()
        if name.lower() in _DOC_TYPE_LEAVES:
            continue
        return name
    return ""


def _fcc_folder_page(callsign: str, service: str, entity_id, download_url: str) -> str:
    """Deep link to the FCC folder that contains this filing. FCC's browse UI
    resolves a folder by the GUID embedded in the download URL alone (path
    segments are cosmetic), so we land the reader on the exact folder, not the
    station root. Falls back to the station root if the GUID can't be parsed.
    Either way it loads reliably (FCC serves normal page navigations to real
    browsers) and establishes the session that makes the direct links work."""
    svc = (service or "").upper()
    slug = _SERVICE_SLUG.get(svc, "tv-profile")
    # cable-/dbs-profile URLs are keyed by the entity id (PSID / provider name);
    # broadcast profiles by callsign.
    ident = str(entity_id) if svc in ("CABLE", "DBS") and entity_id else str(callsign).lower()
    base = f"https://publicfiles.fcc.gov/{slug}/{ident}/political-files"
    m = _DOWNLOAD_FOLDER_RE.search(download_url or "")
    return f"{base}/{m.group(1)}" if m else base


def _direct_url(download_url: str, file_name: str) -> str:
    """Correct the stored download URL's extension at display time. Older
    rows were written with a hardcoded '.pdf' even for Word docs; the real
    extension comes from the filename. (New rows are stored correctly.)"""
    url = download_url or ""
    fn = file_name or ""
    if url.endswith(".pdf") and "." in fn:
        ext = fn.rsplit(".", 1)[-1]
        if ext and ext.lower() != "pdf":
            url = url[:-4] + "." + ext
    return url


st.set_page_config(page_title="Bay Area Political Ad Filings", page_icon="🗳️", layout="wide")

st.title("🗳️ Bay Area Political Ad Filings")
st.caption(
    "Political-file documents filed with the FCC since Jan 1 2025, refreshed "
    "automatically 3×/day. **Coverage** is either *Bay Area* (broadcast + Comcast "
    "cable, market-specific) or *Statewide (CA)* (AT&T U-verse & DirecTV, which "
    "file only by state — California-wide, not Bay-Area-specific). Click a row's "
    "link to open the source PDF on the FCC site; dollar amounts live inside "
    "those PDFs and aren't extracted here (yet)."
)

df = load_filings()
if df.empty:
    st.warning("No filings found. If this is unexpected, the DATABASE_URL secret may be missing or wrong.")
    st.stop()

_PROVIDER_TYPE = {"CABLE": "Cable", "DBS": "Satellite"}
df["race_type"] = df["category_path"].map(_race_type)
df["provider_type"] = df["service"].map(lambda s: _PROVIDER_TYPE.get(str(s).upper(), "Broadcast"))
# Coverage tier: broadcast + Comcast cable are Bay-Area-specific; national
# providers (AT&T/DirecTV/Dish) file only by state and are tagged statewide.
df["coverage"] = df["market"].map(lambda m: "Statewide (CA)" if "statewide" in str(m).lower() else "Bay Area")
# Re-derive the advertiser/committee from the category path (prefix strip +
# document-type step-up) so rows stored before the ingest fix are corrected
# here too, instead of trusting the stored value.
df["purchaser"] = df["category_path"].map(_purchaser)
df["fcc_page"] = df.apply(lambda r: _fcc_folder_page(r["callsign"], r["service"], r["entity_id"], r["download_url"]), axis=1)
df["direct"] = df.apply(lambda r: _direct_url(r["download_url"], r["file_name"]), axis=1)

# --- Filters ---
# Date bounds for the range picker (fall back to today if somehow no dates).
_valid_dates = df["filed_date"].dropna()
min_d = _valid_dates.min().date() if not _valid_dates.empty else date.today()
max_d = _valid_dates.max().date() if not _valid_dates.empty else date.today()

# Row 1: the category-style filters.
r1a, r1b, r1c, r1d = st.columns(4)
with r1a:
    coverages = sorted(df["coverage"].dropna().unique())
    picked_coverage = st.multiselect("Coverage", coverages, default=[],
                                     help="Bay Area = broadcast + Comcast cable (market-specific). "
                                          "Statewide (CA) = AT&T/DirecTV, filed only by state.")
with r1b:
    ptypes = sorted(df["provider_type"].dropna().unique())
    picked_ptypes = st.multiselect("Type", ptypes, default=[])
with r1c:
    stations = sorted(df["callsign"].dropna().unique())
    picked_stations = st.multiselect("Station / system", stations, default=[])
with r1d:
    types = sorted(df["race_type"].dropna().unique())
    picked_types = st.multiselect("Race / category", types, default=[])

# Row 2: advertiser/committee picker, date range, and free-text search.
r2a, r2b, r2c, r2d = st.columns([3, 1.5, 1.5, 3])
with r2a:
    advertisers = sorted(a for a in df["purchaser"].dropna().unique() if a)
    picked_advertisers = st.multiselect("Advertiser / committee", advertisers, default=[],
                                        help="Pick one or more committees to see all their filings.")
with r2b:
    d_from = st.date_input("Filed from", value=min_d, min_value=min_d, max_value=max_d, format="YYYY-MM-DD")
with r2c:
    d_to = st.date_input("Filed to", value=max_d, min_value=min_d, max_value=max_d, format="YYYY-MM-DD")
with r2d:
    query = st.text_input("Search advertiser or document name", "")

view = df
if picked_coverage:
    view = view[view["coverage"].isin(picked_coverage)]
if picked_ptypes:
    view = view[view["provider_type"].isin(picked_ptypes)]
if picked_stations:
    view = view[view["callsign"].isin(picked_stations)]
if picked_types:
    view = view[view["race_type"].isin(picked_types)]
if picked_advertisers:
    view = view[view["purchaser"].isin(picked_advertisers)]
# Only apply the date filter when the user actually narrows the range, so
# undated rows stay visible by default.
if d_from > min_d or d_to < max_d:
    _dd = view["filed_date"].dt.date
    view = view[_dd.notna() & (_dd >= d_from) & (_dd <= d_to)]
if query.strip():
    q = query.strip().lower()
    mask = (
        view["purchaser"].fillna("").str.lower().str.contains(q)
        | view["file_name"].fillna("").str.lower().str.contains(q)
        | view["category_path"].fillna("").str.lower().str.contains(q)
    )
    view = view[mask]

m1, m2, m3 = st.columns(3)
m1.metric("Filings shown", f"{len(view):,}")
m2.metric("Advertisers / committees", f"{view['purchaser'].nunique():,}")
if not view["filed_date"].isna().all():
    m3.metric("Most recent filing", view["filed_date"].max().strftime("%b %d, %Y"))

st.dataframe(
    view[["filed_date", "coverage", "provider_type", "callsign", "purchaser", "race_type", "file_name", "direct", "fcc_page"]],
    hide_index=True,
    use_container_width=True,
    column_config={
        "filed_date": st.column_config.DatetimeColumn("Filed", format="YYYY-MM-DD"),
        "coverage": "Coverage",
        "provider_type": "Type",
        "callsign": "Station / system",
        "purchaser": "Advertiser / committee",
        "race_type": "Race / category",
        "file_name": "Document",
        "direct": st.column_config.LinkColumn("Direct file", display_text="Open ↗"),
        "fcc_page": st.column_config.LinkColumn("FCC page", display_text="Browse ↗"),
    },
)

st.info(
    "**Direct file** opens the document straight from the FCC. If it says "
    "\"Access Denied\", that's FCC's bot-protection blocking cold outside links — "
    "click **FCC page** first (or visit publicfiles.fcc.gov) to establish an FCC "
    "session, then the Direct file links work.",
    icon="ℹ️",
)

st.caption(
    f"{len(df):,} total filings in the database. "
    "Data source: FCC Online Public Inspection File (publicfiles.fcc.gov)."
)
