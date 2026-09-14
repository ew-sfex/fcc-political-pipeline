"""SQLAlchemy models + engine. Works with sqlite:// (local/dev) or a
postgres:// URL (recommended for production - e.g. Supabase) via DATABASE_URL.
"""
import logging
from datetime import datetime, timezone

from sqlalchemy import (
    create_engine, Column, Integer, String, Text, DateTime, Float, Boolean,
    UniqueConstraint, text,
)
from sqlalchemy.orm import declarative_base, sessionmaker

log = logging.getLogger("db")

from . import config

Base = declarative_base()


class Filing(Base):
    """One row per FCC filing document we've ingested.

    Columns are split into two groups:
      - ingestion metadata (populated by Phase 1 / ingest.py)
      - extraction fields (populated later by the Phase 2 LLM extraction step;
        nullable for now so Phase 1 can run standalone)
    """
    __tablename__ = "filings"
    __table_args__ = (
        UniqueConstraint("fcc_file_id", name="uq_fcc_file_id"),
    )

    id = Column(Integer, primary_key=True)

    # --- FCC identifiers ---
    fcc_file_id = Column(String, nullable=False)       # GUID from RSS <id>

    # --- Station / market tagging ---
    callsign = Column(String, nullable=False, index=True)  # display label (e.g. "KGO-TV", "Comcast (Bay Area)")
    service = Column(String)                            # TV / AM / FM / CABLE
    entity_id = Column(String, index=True)             # FCC facility id (broadcast) or PSID (cable) - used to deep-link to the owning profile
    market = Column(String, index=True)
    category_path = Column(Text)                         # e.g. "Political Files/2026/Non-Candidate Issue Ads/BOLD America"
    campaign_year = Column(String)

    # --- File info ---
    file_name = Column(String)
    filed_date = Column(DateTime)                        # <updated> from FCC RSS entry
    download_url = Column(Text)                           # direct FCC PDF link (for reference/re-download)

    # --- Storage ---
    drive_file_id = Column(String)
    drive_web_link = Column(Text)

    # --- Purchaser: parsed directly from category_path at ingest time (the
    # RSS feed's category taxonomy already ends in the advertiser/committee
    # name) - populated at ingest, no LLM needed for this field. ---
    purchaser = Column(String, index=True)

    # --- Phase 2: extraction of amounts/dates/doc-type from the PDF body
    # itself (nullable until populated) ---
    document_type = Column(String)                       # INVOICE / ORDER / CONTRACT / OTHER
    gross_amount = Column(Float)
    flight_start = Column(DateTime)
    flight_end = Column(DateTime)
    extraction_confidence = Column(Float)
    needs_review = Column(Boolean, default=False)
    extraction_raw_json = Column(Text)                    # full LLM output, for audit

    ingested_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


_engine = None
_SessionLocal = None


def get_engine():
    global _engine
    if _engine is None:
        url = config.DATABASE_URL
        # Supabase/Heroku hand out "postgres://..." URLs, but SQLAlchemy only
        # recognizes the "postgresql://" scheme (bare "postgres" fails with a
        # "Can't load plugin" error). Normalize so either form just works.
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        _engine = create_engine(url, future=True)
    return _engine


def get_session():
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), future=True)
    return _SessionLocal()


def init_db():
    Base.metadata.create_all(get_engine())
    _ensure_columns()


# Columns added after the table was first created in production. create_all()
# only creates missing *tables*, never alters existing ones, so new columns
# need an explicit (idempotent) ALTER. Keeps the live Supabase table in sync
# without a manual migration step.
_ADDED_COLUMNS = {
    "entity_id": "VARCHAR",
}


def _ensure_columns():
    engine = get_engine()
    for name, coltype in _ADDED_COLUMNS.items():
        try:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE filings ADD COLUMN {name} {coltype}"))
            log.info("Added missing column filings.%s", name)
        except Exception:
            # Already exists (the normal case) - SQLite raises "duplicate
            # column", Postgres "already exists". Anything else surfaces on the
            # next real query; a missing column isn't worth failing startup for.
            pass
