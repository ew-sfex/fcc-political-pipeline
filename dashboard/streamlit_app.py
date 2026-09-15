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
# Strip the cable ad-platform prefix ("AMP - ", "POL - ", ...) for any rows
# stored before ingest did this itself; new rows are already clean.
df["purchaser"] = df["purchaser"].fillna("").str.replace(r"^[A-Z]{2,5} - ", "", regex=True)
df["fcc_page"] = df.apply(lambda r: _fcc_folder_page(r["callsign"], r["service"], r["entity_id"], r["download_url"]), axis=1)
df["direct"] = df.apply(lambda r: _direct_url(r["download_url"], r["file_name"]), axis=1)

# --- Filters ---
cc, c0, c1, c2, c3 = st.columns([1.5, 1.3, 2, 1.8, 2.4])
with cc:
    coverages = sorted(df["coverage"].dropna().unique())
    picked_coverage = st.multiselect("Coverage", coverages, default=[],
                                     help="Bay Area = broadcast + Comcast cable (market-specific). "
                                          "Statewide (CA) = AT&T/DirecTV, filed only by state.")
with c0:
    ptypes = sorted(df["provider_type"].dropna().unique())
    picked_ptypes = st.multiselect("Type", ptypes, default=[])
with c1:
    stations = sorted(df["callsign"].dropna().unique())
    picked_stations = st.multiselect("Station / system", stations, default=[])
with c2:
    types = sorted(df["race_type"].dropna().unique())
    picked_types = st.multiselect("Race / category", types, default=[])
with c3:
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
