"""SQLAlchemy models + engine. Works with sqlite:// (local/dev) or a
postgres:// URL (recommended for production - e.g. Supabase) via DATABASE_URL.
"""
from datetime import datetime, timezone

from sqlalchemy import (
    create_engine, Column, Integer, String, Text, DateTime, Float, Boolean,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, sessionmaker

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
    callsign = Column(String, nullable=False, index=True)
    service = Column(String)                            # TV / AM / FM
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
        _engine = create_engine(config.DATABASE_URL, future=True)
    return _engine


def get_session():
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), future=True)
    return _SessionLocal()


def init_db():
    Base.metadata.create_all(get_engine())
