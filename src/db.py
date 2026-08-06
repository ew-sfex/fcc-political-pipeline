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
    fcc_file_id = Column(String, nullable=False)       # file_id from FCC API
    folder_id = Column(String)
    file_manager_id = Column(String)
    entity_id = Column(String)

    # --- Station / market tagging ---
    callsign = Column(String, nullable=False, index=True)
    service = Column(String)                            # TV / AM / FM
    market = Column(String, index=True)
    political_file_type = Column(String)
    office_type = Column(String)
    campaign_year = Column(String)

    # --- File info ---
    file_name = Column(String)
    file_extension = Column(String)
    filed_date = Column(DateTime)                        # create_ts from FCC

    # --- Storage ---
    drive_file_id = Column(String)
    drive_web_link = Column(Text)

    # --- Phase 2: extraction (nullable until populated) ---
    purchaser = Column(String, index=True)               # advertiser/agency, once extracted
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
